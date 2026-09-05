"""Phase 2: OrderExecutor limit-price math, fill settle, idempotency, and safety gates."""
import pytest

from agentic.brokers.base import BrokerCapabilities, ExecutionBroker
from agentic.brokers.paper_broker import PaperBroker
from agentic.config import Settings
from agentic.domain.enums import DecisionStatus, OrderStatus, PositionStatus, RuleType
from agentic.domain.models import CloseDecision, Order, Position
from agentic.marketdata.base import PaperMarketData
from agentic.marketdata.quote import OptionQuote
from agentic.services.executor import OrderExecutor, compute_limit_price, round_to_tick
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore


# ----------------------------------------------------------------- limit price math
def test_round_to_tick():
    assert round_to_tick(1.234, 0.01) == 1.23
    assert round_to_tick(1.236, 0.01) == 1.24
    assert round_to_tick(2.07, 0.05) == 2.05


def test_limit_price_mid_plus_buffer():
    q = OptionQuote("X", bid=1.00, ask=1.10, mark=1.05)  # mid = 1.05
    # 1.05 * 1.02 = 1.071 -> 1.07; cap = 1.10*1.05 = 1.155, not binding.
    assert compute_limit_price(q, buffer_pct=0.02, slippage_cap_pct=0.05) == 1.07


def test_limit_price_respects_slippage_cap():
    q = OptionQuote("X", bid=1.00, ask=1.02, mark=1.01)  # mid = 1.01
    # buffer would give 1.01*1.20 = 1.212, but cap = 1.02*1.05 = 1.071 -> 1.07.
    assert compute_limit_price(q, buffer_pct=0.20, slippage_cap_pct=0.05) == 1.07


def test_limit_price_none_when_unpriceable():
    assert compute_limit_price(OptionQuote("X", None, None, None),
                               buffer_pct=0.02, slippage_cap_pct=0.05) is None


# ----------------------------------------------------------------- wiring fixtures
@pytest.fixture()
def wiring(tmp_path):
    db = Database(tmp_path / "exec.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    killswitch = KillSwitch(db, audit)
    return db, audit, positions, decisions, orders, killswitch


def _seed_position(positions: PositionStore, broker=None) -> Position:
    from agentic.brokers import paper_broker as pb
    pos = pb._default_seed()[0]
    positions.upsert(pos)
    return positions.get_by_occ(pos.occ_symbol)


def _decision(pos: Position, requires_approval: bool = False) -> CloseDecision:
    d = CloseDecision(
        position_id=pos.id, rule_name="profit-50", rule_type=RuleType.PROFIT_TARGET,
        reason="test", requires_approval=requires_approval, dedup_key=f"{pos.id}:PT:today",
    )
    return d


def _make_executor(wiring, broker, settings, market_data=None):
    db, audit, positions, decisions, orders, killswitch = wiring
    return OrderExecutor(
        settings, broker, market_data or PaperMarketData(), positions, orders, decisions,
        audit, killswitch, poll_interval_seconds=0.001,
    )


# ----------------------------------------------------------------- end-to-end
@pytest.mark.asyncio
async def test_execute_close_fills_and_settles(wiring):
    db, audit, positions, decisions, orders, killswitch = wiring
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    pos = _seed_position(positions, broker)
    decision = _decision(pos)
    decisions.insert_if_new(decision)

    ex = _make_executor(wiring, broker, settings)
    order = await ex.execute_close(pos, decision)

    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert positions.get_by_occ(pos.occ_symbol).status == PositionStatus.CLOSED
    assert decisions.get(decision.id).status == DecisionStatus.DONE
    assert any(e["event_type"] == "ORDER_FILL" for e in audit.recent())


@pytest.mark.asyncio
async def test_execute_close_is_idempotent(wiring):
    db, audit, positions, decisions, orders, killswitch = wiring
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    pos = _seed_position(positions, broker)
    decision = _decision(pos)
    decisions.insert_if_new(decision)
    ex = _make_executor(wiring, broker, settings)

    first = await ex.execute_close(pos, decision)
    second = await ex.execute_close(pos, decision)
    assert first.client_order_id == second.client_order_id
    # Exactly one order row exists for this decision's client_order_id.
    assert orders.get_by_client_order_id(f"close-{decision.id}") is not None
    assert second.status == OrderStatus.FILLED


@pytest.mark.asyncio
async def test_killswitch_blocks_execution(wiring):
    db, audit, positions, decisions, orders, killswitch = wiring
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    pos = _seed_position(positions, broker)
    decision = _decision(pos)
    decisions.insert_if_new(decision)
    killswitch.pause("test")

    ex = _make_executor(wiring, broker, settings)
    result = await ex.execute_close(pos, decision)
    assert result is None
    assert orders.get_by_client_order_id(f"close-{decision.id}") is None
    assert positions.get_by_occ(pos.occ_symbol).status == PositionStatus.OPEN


@pytest.mark.asyncio
async def test_stale_quote_blocks_execution(wiring):
    db, audit, positions, decisions, orders, killswitch = wiring
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    pos = _seed_position(positions, broker)
    decision = _decision(pos)
    decisions.insert_if_new(decision)

    class StaleData(PaperMarketData):
        async def get_quote(self, position):
            from datetime import timedelta
            from agentic.domain.models import utcnow
            return OptionQuote(position.occ_symbol, 1.0, 1.1, 1.05,
                               as_of=utcnow() - timedelta(seconds=999))

    ex = _make_executor(wiring, broker, settings, market_data=StaleData())
    result = await ex.execute_close(pos, decision)
    assert result is None
    assert orders.get_by_client_order_id(f"close-{decision.id}") is None


@pytest.mark.asyncio
async def test_live_broker_not_armed_does_not_submit(wiring):
    """A non-paper broker must NOT place an order unless settings.is_live."""
    db, audit, positions, decisions, orders, killswitch = wiring

    class FakeLiveBroker(ExecutionBroker):
        def __init__(self):
            self.submitted = False
        async def connect(self): ...
        def capabilities(self):
            return BrokerCapabilities("fake_live", supports_options_orders=True, is_paper=False)
        async def get_open_positions(self): return []
        async def submit_close_order(self, order):
            self.submitted = True
            order.status = OrderStatus.FILLED
            return order
        async def get_order(self, order): return order
        async def cancel_order(self, order): ...

    broker = FakeLiveBroker()
    # mode=paper -> not armed even though broker is "live".
    settings = Settings(mode="paper", broker="robinhood_mcp", market_data="paper")
    pos = _seed_position(positions, broker)
    decision = _decision(pos)
    decisions.insert_if_new(decision)

    ex = _make_executor(wiring, broker, settings)
    result = await ex.execute_close(pos, decision)
    assert result is None
    assert broker.submitted is False
    assert orders.get_by_client_order_id(f"close-{decision.id}") is None
