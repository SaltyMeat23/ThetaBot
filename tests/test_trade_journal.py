"""Trade journal: store basics, IV-history upsert, entry capture, outcome backfill."""
from datetime import date, timedelta

import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, EntryCriteria, Settings
from agentic.domain.enums import Direction, OptionType, OrderStatus, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Order, Position, TradeJournalEntry, utcnow
from agentic.marketdata.base import MarketDataProvider, PaperMarketData
from agentic.marketdata.quote import OptionContractQuote
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
from agentic.store.trade_journal import TradeJournalStore


def _stores(tmp_path, name="j.db"):
    db = Database(tmp_path / name)
    return (db, AuditStore(db), PositionStore(db), DecisionStore(db), OrderStore(db),
            EntryDecisionStore(db), TradeJournalStore(db), KillSwitch(db, AuditStore(db)))


def test_store_insert_find_outcome(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    e = TradeJournalEntry(occ_symbol="X1", underlying="X", kind="CSP", contracts=2,
                          strike=50.0, dte=10, delta=-0.25, premium=1.5)
    journal.insert(e)
    found = journal.find_open_by_occ("X1")
    assert found is not None and found.status == "open" and found.contracts == 2
    journal.set_outcome(found.id, status="win", realized_pnl=120.0, exit_reason="profit-50",
                        entered_at=found.entered_at)
    assert journal.find_open_by_occ("X1") is None       # no longer open
    assert journal.recent()[0].status == "win" and journal.recent()[0].realized_pnl == 120.0


def test_set_outcome_records_mfe_mae_into_context(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    e = TradeJournalEntry(occ_symbol="X2", underlying="X", kind="CSP", contracts=1,
                          strike=50.0, dte=10, delta=-0.25, premium=1.5,
                          context={"rsi": 55})           # pre-existing context is preserved
    journal.insert(e)
    fid = journal.find_open_by_occ("X2").id
    journal.set_outcome(fid, status="win", realized_pnl=75.0, entered_at=e.entered_at,
                        mfe_pct=0.82, mae_pct=-0.15)      # reached +82%, dipped to -15%
    ctx = journal.recent()[0].context
    assert ctx["mfe_pct"] == 0.82 and ctx["mae_pct"] == -0.15
    assert ctx["rsi"] == 55                              # original context not clobbered


def test_iv_history_upsert(tmp_path):
    *_, journal, _ks = _stores(tmp_path)
    journal.record_iv("X", date(2026, 6, 30), 0.45)
    journal.record_iv("X", date(2026, 6, 30), 0.50)   # same day -> upsert, not duplicate
    journal.record_iv("X", date(2026, 6, 29), 0.40)
    hist = journal.iv_history("X")
    assert len(hist) == 2
    assert dict(hist)["2026-06-30"] == 0.50


def test_executor_journal_outcome_backfill(tmp_path):
    db, audit, positions, decisions, orders, _ed, journal, killswitch = _stores(tmp_path)
    journal.insert(TradeJournalEntry(occ_symbol="XP", underlying="X", kind="CSP", contracts=1,
                                     strike=45.0, dte=9, delta=-0.25, premium=1.62))
    pos = Position(occ_symbol="XP", underlying="X", option_type=OptionType.PUT,
                   strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
                   strike=45.0, expiration=utcnow().date() + timedelta(days=9),
                   credit_received=1.62, status=PositionStatus.OPEN)
    close_order = Order(decision_id="d", position_id="p", occ_symbol="XP", quantity=1,
                        limit_price=0.10, is_paper=True, status=OrderStatus.FILLED,
                        avg_fill_price=0.10)
    ex = OrderExecutor(Settings(mode="paper"), PaperBroker(), PaperMarketData(), positions,
                       orders, decisions, audit, killswitch, trade_journal=journal)
    ex._journal_outcome(pos, close_order, "profit-50")
    je = journal.recent()[0]
    assert je.status == "win" and je.realized_pnl == 152.0  # (1.62 - 0.10) * 100
    assert je.exit_reason == "profit-50" and je.days_held is not None


@pytest.mark.asyncio
async def test_reconcile_assignment_backfills_journal(tmp_path):
    db, audit, positions, *_rest, journal, _ks = _stores(tmp_path)
    exp = utcnow().date() + timedelta(days=5)
    occ = "X" + exp.strftime("%y%m%d") + "P00045000"
    positions.upsert(Position(occ_symbol=occ, underlying="X", option_type=OptionType.PUT,
                              strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT,
                              quantity=1, strike=45.0, expiration=exp, credit_received=1.0,
                              status=PositionStatus.OPEN))
    journal.insert(TradeJournalEntry(occ_symbol=occ, underlying="X", kind="CSP", contracts=1,
                                     strike=45.0, dte=5, delta=-0.25, premium=1.0))
    broker = PaperBroker(seed_positions=[], holdings=[EquityHolding("X", 100, 45.0)])
    reconcile = ReconcileLoop(Settings(mode="paper"), broker, positions, audit,
                              trade_journal=journal)
    await reconcile.run_once()
    assert journal.recent()[0].status == "assigned"


class _PutChain(MarketDataProvider):
    def __init__(self, chain):
        self._chain = chain

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return self._chain if underlying == "X" else []

    async def get_underlying_price(self, underlying):
        return 48.0


@pytest.mark.asyncio
async def test_scanner_fill_creates_journal_row(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    db, audit, positions, decisions, orders, entry_decisions, journal, killswitch = _stores(tmp_path)
    exp = utcnow().date() + timedelta(days=10)
    put = OptionContractQuote(
        occ_symbol="X" + exp.strftime("%y%m%d") + "P00050000", underlying="X", option_id=None,
        option_type="put", strike=50.0, expiration=exp, bid=1.60, ask=1.70, mark=1.65,
        delta=-0.25, iv=0.45, open_interest=500, volume=50,
    )
    md = _PutChain([put])
    settings = Settings(mode="paper", broker="paper", entry=EntryConfig(
        enabled=True, watchlist=["X"],
        criteria=EntryCriteria(delta_min=0.20, delta_max=0.30, dte_min=7, dte_max=14,
                               min_annualized_yield=0.10, min_open_interest=100, min_volume=10,
                               max_spread_pct=0.15, exclude_earnings_days=0)))
    broker = PaperBroker(seed_positions=[], buying_power=100_000.0)
    ex = OrderExecutor(settings, broker, md, positions, orders, decisions, audit, killswitch,
                       entry_decisions=entry_decisions, trade_journal=journal,
                       poll_interval_seconds=0.001)
    scanner = OpportunityScanner(settings, broker, md, entry_decisions, ex, audit, killswitch,
                                 trade_journal=journal)
    await scanner.run_once()
    je = journal.recent()
    assert len(je) == 1
    row = je[0]
    assert row.kind == "CSP" and row.status == "open" and row.delta == -0.25
    assert row.underlying_price == 48.0 and row.iv == 0.45
    # IV history was seeded for the scanned symbol.
    assert journal.iv_history("X")
