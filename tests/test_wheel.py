"""Wheel: covered-call entry end-to-end on paper, and CSP-assignment detection in reconcile."""
from datetime import date, timedelta

import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, EntryCriteria, Settings
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Position, utcnow
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.notify.base import Notifier
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.reconcile import ReconcileLoop
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore


def _occ_call(underlying, exp, strike):
    return f"{underlying}{exp:%y%m%d}C{int(strike * 1000):08d}"


class CallChainData(MarketDataProvider):
    def __init__(self, symbol, chain):
        self._symbol, self._chain = symbol, chain

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return self._chain if underlying == self._symbol else []


class CapturingNotifier(Notifier):
    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, priority="normal"):
        self.sent.append((title, message, priority))


@pytest.mark.asyncio
async def test_covered_call_entry_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    db = Database(tmp_path / "cc.db")
    audit, positions, decisions = AuditStore(db), PositionStore(db), DecisionStore(db)
    orders, entry_decisions = OrderStore(db), EntryDecisionStore(db)
    killswitch = KillSwitch(db, audit)

    exp = utcnow().date() + timedelta(days=35)
    call = OptionContractQuote(
        occ_symbol=_occ_call("X", exp, 45), underlying="X", option_id=None, option_type="call",
        strike=45.0, expiration=exp, bid=1.00, ask=1.10, mark=1.05, delta=0.25, iv=0.4,
        open_interest=500, volume=50,
    )
    broker = PaperBroker(seed_positions=[], holdings=[EquityHolding("X", 100, 40.0)])
    market_data = CallChainData("X", [call])
    settings = Settings(mode="paper", broker="paper", entry=EntryConfig(
        enabled=True, watchlist=[],   # no CSP pass; CC pass runs off holdings
        cc_criteria=EntryCriteria(delta_min=0.20, delta_max=0.30, dte_min=30, dte_max=45,
                                  min_annualized_yield=0.10, min_open_interest=100,
                                  min_volume=10, max_spread_pct=0.15, exclude_earnings_days=0),
    ))
    executor = OrderExecutor(settings, broker, market_data, positions, orders, decisions,
                             audit, killswitch, entry_decisions=entry_decisions,
                             poll_interval_seconds=0.001)
    scanner = OpportunityScanner(settings, broker, market_data, entry_decisions, executor,
                                 audit, killswitch)

    submitted = await scanner.run_once()
    assert submitted == 1

    d = entry_decisions.recent()[0]
    assert d.rule_name == "cc-screener" and d.contracts == 1 and d.strike == 45.0
    # The new short call (covered call) now exists at the broker.
    pos = await broker.get_open_positions()
    cc = [p for p in pos if p.underlying == "X" and p.option_type is OptionType.CALL]
    assert len(cc) == 1 and cc[0].strategy is Strategy.COVERED_CALL
    assert cc[0].direction is Direction.SHORT


@pytest.mark.asyncio
async def test_reconcile_detects_csp_assignment_and_notifies(tmp_path):
    db = Database(tmp_path / "asn.db")
    audit, positions = AuditStore(db), PositionStore(db)
    exp = utcnow().date() + timedelta(days=5)
    put = Position(
        occ_symbol="X" + exp.strftime("%y%m%d") + "P00045000", underlying="X",
        option_type=OptionType.PUT, strategy=Strategy.CASH_SECURED_PUT,
        direction=Direction.SHORT, quantity=1, strike=45.0, expiration=exp,
        credit_received=1.0, status=PositionStatus.OPEN,
    )
    positions.upsert(put)
    # Broker: the short put is GONE (assigned) and we now hold 100 shares of X.
    broker = PaperBroker(seed_positions=[], holdings=[EquityHolding("X", 100, 45.0)])
    notifier = CapturingNotifier()
    reconcile = ReconcileLoop(Settings(mode="paper"), broker, positions, audit,
                              notifier=notifier)

    await reconcile.run_once()

    assert positions.get_by_occ(put.occ_symbol).status is PositionStatus.ASSIGNED
    assert any("ASSIGNED" in t for t, _m, _p in notifier.sent)
