"""Phase 4: reconcile hardening (classify gone positions, qty drift, orphan orders) +
auto-trip kill switch."""
from datetime import date, timedelta

import pytest

from agentic.brokers.base import BrokerCapabilities, ExecutionBroker
from agentic.config import Settings
from agentic.domain.enums import (
    Direction,
    OptionType,
    OrderStatus,
    PositionStatus,
    Strategy,
)
from agentic.domain.models import Order, Position
from agentic.services.killswitch import KillSwitch
from agentic.services.reconcile import ReconcileLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore


class FakeBroker(ExecutionBroker):
    def __init__(self, open_positions=None, order_states=None):
        self._open = open_positions or []
        self._order_states = order_states or {}  # client_order_id -> Order
    async def connect(self): ...
    def capabilities(self):
        return BrokerCapabilities("fake", supports_options_orders=True, is_paper=True)
    async def get_open_positions(self):
        return list(self._open)
    async def submit_close_order(self, order):
        return order
    async def get_order(self, order):
        return self._order_states.get(order.client_order_id, order)
    async def cancel_order(self, order): ...


def _pos(occ, underlying, dte_days, qty=1) -> Position:
    return Position(
        occ_symbol=occ, underlying=underlying, option_type=OptionType.CALL,
        strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=qty,
        strike=100.0, expiration=date.today() + timedelta(days=dte_days), credit_received=1.0,
    )


@pytest.fixture()
def wiring(tmp_path):
    db = Database(tmp_path / "rec.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    orders = OrderStore(db)
    settings = Settings()
    return db, audit, positions, orders, settings


@pytest.mark.asyncio
async def test_gone_positions_classified_expired_vs_closed(wiring):
    db, audit, positions, orders, settings = wiring
    positions.upsert(_pos("EXP240101C00100000", "EXP", dte_days=-3))   # past expiration
    positions.upsert(_pos("LIVE250101C00100000", "LIV", dte_days=10))  # still alive
    broker = FakeBroker(open_positions=[])  # both vanished from broker

    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders)
    diff = await rec.run_once()

    statuses = {d["occ"]: d["status"] for d in diff["gone"]}
    assert statuses["EXP240101C00100000"] == PositionStatus.EXPIRED.value
    assert statuses["LIVE250101C00100000"] == PositionStatus.CLOSED.value


@pytest.mark.asyncio
async def test_quantity_drift_detected_and_synced(wiring):
    db, audit, positions, orders, settings = wiring
    positions.upsert(_pos("AAPL250101C00100000", "AAPL", dte_days=10, qty=3))
    broker = FakeBroker(open_positions=[_pos("AAPL250101C00100000", "AAPL", 10, qty=1)])

    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders)
    diff = await rec.run_once()

    assert diff["qty_drift"] == [{"occ": "AAPL250101C00100000", "from": 3, "to": 1}]
    assert positions.get_by_occ("AAPL250101C00100000").quantity == 1


@pytest.mark.asyncio
async def test_orphan_order_finalized_and_closes_position(wiring):
    db, audit, positions, orders, settings = wiring
    pos = _pos("AAPL250101C00100000", "AAPL", dte_days=10)
    positions.upsert(pos)
    pos = positions.get_by_occ(pos.occ_symbol)

    stuck = Order(decision_id="d1", position_id=pos.id, occ_symbol=pos.occ_symbol,
                  quantity=1, limit_price=0.5, is_paper=True, client_order_id="coid-1",
                  status=OrderStatus.SUBMITTED, broker_order_id="b1")
    orders.insert_if_new(stuck)

    filled = Order(decision_id="d1", position_id=pos.id, occ_symbol=pos.occ_symbol,
                   quantity=1, limit_price=0.5, is_paper=True, client_order_id="coid-1",
                   status=OrderStatus.FILLED, broker_order_id="b1", filled_qty=1,
                   avg_fill_price=0.5)
    broker = FakeBroker(open_positions=[pos], order_states={"coid-1": filled})

    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders)
    diff = await rec.run_once()

    assert diff["orphans_finalized"] == [{"client_order_id": "coid-1", "status": "FILLED"}]
    assert orders.get_by_client_order_id("coid-1").status == OrderStatus.FILLED
    assert positions.get_by_occ(pos.occ_symbol).status == PositionStatus.CLOSED


@pytest.mark.asyncio
async def test_heals_false_failed_entry_decision(wiring):
    """A sub-second fill can leave the entry decision FAILED while the short is actually open at
    the broker. Reconcile must heal it to DONE (the observed live ONDS case)."""
    from agentic.domain.enums import DecisionStatus
    from agentic.domain.models import EntryDecision
    from agentic.store.entry_decisions import EntryDecisionStore

    db, audit, positions, orders, settings = wiring
    entry_decisions = EntryDecisionStore(db)
    occ = "ONDS260731P00007500"
    d = EntryDecision(
        underlying="ONDS", occ_symbol=occ, option_id=None, strike=7.5,
        expiration=date(2026, 7, 31), contracts=1, premium=0.2, rule_name="csp-screener",
        reason="CSP", dedup_key="ONDS:2026-07-31:7.5:2026-07-22",
    )
    entry_decisions.insert_if_new(d)
    entry_decisions.set_status(d.id, DecisionStatus.FAILED)  # the phantom failure

    # Broker shows the position OPEN (authoritative).
    broker = FakeBroker(open_positions=[_pos(occ, "ONDS", dte_days=7)])
    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders,
                        entry_decisions=entry_decisions)
    diff = await rec.run_once()

    assert diff["entry_decisions_healed"] == [
        {"occ": occ, "decision_id": d.id, "from": "FAILED"}
    ]
    assert entry_decisions.get(d.id).status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_heal_leaves_rejected_and_done_untouched(wiring):
    """Only FAILED/EXECUTING heal. A user-REJECTED decision on an open contract is NOT flipped
    (could be a manual open), and an already-DONE one is left alone."""
    from agentic.domain.enums import DecisionStatus
    from agentic.domain.models import EntryDecision
    from agentic.store.entry_decisions import EntryDecisionStore

    db, audit, positions, orders, settings = wiring
    entry_decisions = EntryDecisionStore(db)
    occ = "AAA260731P00010000"
    d = EntryDecision(
        underlying="AAA", occ_symbol=occ, option_id=None, strike=10.0,
        expiration=date(2026, 7, 31), contracts=1, premium=0.5, rule_name="csp-screener",
        reason="CSP", dedup_key="AAA:2026-07-31:10:2026-07-22",
    )
    entry_decisions.insert_if_new(d)
    entry_decisions.set_status(d.id, DecisionStatus.REJECTED)

    broker = FakeBroker(open_positions=[_pos(occ, "AAA", dte_days=7)])
    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders,
                        entry_decisions=entry_decisions)
    diff = await rec.run_once()

    assert diff["entry_decisions_healed"] == []
    assert entry_decisions.get(d.id).status == DecisionStatus.REJECTED


@pytest.mark.asyncio
async def test_reconcile_skips_when_broker_degraded(wiring):
    """Live-armed but on the paper simulator: reconcile must NOT sync (would mark real positions
    gone/closed against the wrong account — the ONDS corruption)."""
    db, audit, positions, orders, _settings = wiring
    positions.upsert(_pos("REAL250101C00100000", "REAL", dte_days=10))  # a real open position
    degraded_settings = Settings(mode="live", i_understand_live_trading=True,
                                 broker="robinhood_mcp", market_data="paper")
    broker = FakeBroker(open_positions=[])  # paper broker (is_paper=True) reports nothing

    rec = ReconcileLoop(degraded_settings, broker, positions, audit, orders=orders)
    diff = await rec.run_once()

    assert diff.get("degraded") is True
    # The real position survives — NOT reclassified as gone/closed.
    assert positions.get_by_occ("REAL250101C00100000").status == PositionStatus.OPEN


def test_auto_trip_killswitch(wiring):
    db, audit, positions, orders, settings = wiring
    ks = KillSwitch(db, audit, auto_trip_threshold=2)
    ks.record_broker_error("monitor")
    assert ks.is_paused() is False
    ks.record_broker_error("monitor")
    assert ks.is_paused() is True  # tripped on the 2nd consecutive error


def test_auto_trip_disabled_by_default(wiring):
    db, audit, positions, orders, settings = wiring
    ks = KillSwitch(db, audit)  # threshold 0 = disabled
    for _ in range(10):
        ks.record_broker_error()
    assert ks.is_paused() is False


@pytest.mark.asyncio
async def test_closed_buyback_journals_estimated_pnl(wiring):
    """A tested short put bought back before expiry (gone from the broker) with no captured fill
    order gets its realized P&L journaled, estimated from the last mark — the ONDS 7.5P gap."""
    from agentic.domain.models import TradeJournalEntry
    from agentic.store.trade_journal import TradeJournalStore
    db, audit, positions, orders, settings = wiring
    journal = TradeJournalStore(db)
    pos = Position(
        occ_symbol="ONDS260731P00007500", underlying="ONDS", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1, strike=7.5,
        expiration=date.today() + timedelta(days=3), credit_received=0.20, current_mark=0.58,
    )
    positions.upsert(pos)
    journal.insert(TradeJournalEntry(occ_symbol=pos.occ_symbol, underlying="ONDS", kind="CSP",
                                     contracts=1, strike=7.5, dte=9, premium=0.20))
    broker = FakeBroker(open_positions=[])  # vanished from the broker (bought back)

    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders, trade_journal=journal)
    diff = await rec.run_once()

    assert {d["occ"]: d["status"] for d in diff["gone"]}["ONDS260731P00007500"] == "CLOSED"
    je = journal.recent(10)[0]
    assert je.status == "closed"
    assert je.realized_pnl == -38.0                 # (0.20 - 0.58) * 100, estimated from the mark
    assert je.close_price == 0.58
    assert je.exit_reason == "reconcile:closed"


@pytest.mark.asyncio
async def test_expired_still_journals_full_credit(wiring):
    """Regression: expiry keeps the full credit (positive), unaffected by the close-P&L change."""
    from agentic.domain.models import TradeJournalEntry
    from agentic.store.trade_journal import TradeJournalStore
    db, audit, positions, orders, settings = wiring
    journal = TradeJournalStore(db)
    pos = Position(
        occ_symbol="F260717P00013000", underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1, strike=13.0,
        expiration=date.today() - timedelta(days=1), credit_received=0.115,  # past expiry
    )
    positions.upsert(pos)
    journal.insert(TradeJournalEntry(occ_symbol=pos.occ_symbol, underlying="F", kind="CSP",
                                     contracts=1, strike=13.0, dte=8, premium=0.115))
    broker = FakeBroker(open_positions=[])

    rec = ReconcileLoop(settings, broker, positions, audit, orders=orders, trade_journal=journal)
    await rec.run_once()

    je = journal.recent(10)[0]
    assert je.status == "expired" and je.realized_pnl == 11.5   # 0.115 * 100 kept
