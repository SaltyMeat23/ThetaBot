"""execute_open refetches a fresh quote when the caller's has gone stale.

Regression guard: enabling the AI review adds seconds between the scan-time quote fetch and
execute_open, ageing the quote past max_quote_age_seconds. An approved entry must survive that.
"""
from datetime import timedelta

import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import Settings
from agentic.domain.enums import DecisionStatus, OrderStatus
from agentic.domain.models import EntryDecision, utcnow
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore

EXP = utcnow().date() + timedelta(days=7)
OCC = "F260724P00014000"


def _quote(as_of=None):
    return OptionContractQuote(
        occ_symbol=OCC, underlying="F", option_id=None, option_type="put",
        strike=14.0, expiration=EXP, bid=0.13, ask=0.14, mark=0.135,
        delta=-0.27, iv=0.39, open_interest=500, volume=228, as_of=as_of or utcnow(),
    )


class FreshChainData(MarketDataProvider):
    """get_chain always returns a fresh quote for F — stands in for a live refetch."""
    def __init__(self):
        self.get_chain_calls = 0

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        self.get_chain_calls += 1
        return [_quote()] if underlying == "F" else []


@pytest.fixture()
def executor(tmp_path):
    db = Database(tmp_path / "x.db")
    audit = AuditStore(db)
    positions, decisions = PositionStore(db), DecisionStore(db)
    orders, entry_decisions = OrderStore(db), EntryDecisionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker(seed_positions=[], buying_power=100_000.0)
    md = FreshChainData()
    settings = Settings(mode="paper", broker="paper")
    ex = OrderExecutor(settings, broker, md, positions, orders, decisions, audit,
                       killswitch, entry_decisions=entry_decisions, poll_interval_seconds=0.001)
    return ex, md, entry_decisions


def _decision():
    return EntryDecision(
        underlying="F", occ_symbol=OCC, option_id=None, strike=14.0, expiration=EXP,
        contracts=1, premium=0.135, rule_name="csp-screener", reason="test", dedup_key="F:test",
    )


@pytest.mark.asyncio
async def test_stale_quote_is_refetched_and_entry_proceeds(executor):
    ex, md, entry_decisions = executor
    d = _decision()
    entry_decisions.insert_if_new(d)
    stale = _quote(as_of=utcnow() - timedelta(seconds=30))   # 30s old > 10s guard
    order = await ex.execute_open(d, stale)
    assert md.get_chain_calls == 1                            # it refetched instead of blocking
    assert order is not None and order.status == OrderStatus.FILLED
    assert entry_decisions.get(d.id).status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_fresh_quote_is_not_refetched(executor):
    ex, md, entry_decisions = executor
    d = _decision()
    entry_decisions.insert_if_new(d)
    order = await ex.execute_open(d, _quote())               # already fresh
    assert md.get_chain_calls == 0                            # no needless refetch
    assert order is not None and order.status == OrderStatus.FILLED
    assert entry_decisions.get(d.id).status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_unconfirmed_fill_confirmed_via_position_read(tmp_path):
    """RH order-state can lag a sub-second fill; the position read is authoritative -> DONE, not FAILED."""
    from agentic.config import ExecutionConfig
    from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
    from agentic.domain.models import Position

    db = Database(tmp_path / "laggy.db")
    audit = AuditStore(db)
    positions, decisions = PositionStore(db), DecisionStore(db)
    orders, entry_decisions = OrderStore(db), EntryDecisionStore(db)
    killswitch = KillSwitch(db, audit)

    class Laggy(PaperBroker):
        async def submit_open_order(self, order):
            order.status = OrderStatus.SUBMITTED     # never reports FILLED via order-state
            return order

        async def get_order(self, order):
            order.status = OrderStatus.SUBMITTED
            return order

        async def get_open_positions(self):
            return [Position(
                occ_symbol=OCC, underlying="F", option_type=OptionType.PUT,
                strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
                strike=14.0, expiration=EXP, credit_received=0.135, status=PositionStatus.OPEN)]

    broker = Laggy(seed_positions=[], buying_power=100_000.0)
    settings = Settings(mode="paper", broker="paper",
                        execution=ExecutionConfig(fill_timeout_seconds=1))
    ex = OrderExecutor(settings, broker, FreshChainData(), positions, orders, decisions, audit,
                       killswitch, entry_decisions=entry_decisions, poll_interval_seconds=0.001)
    d = _decision()
    entry_decisions.insert_if_new(d)
    await ex.execute_open(d, _quote())
    assert entry_decisions.get(d.id).status == DecisionStatus.DONE   # confirmed via position read
