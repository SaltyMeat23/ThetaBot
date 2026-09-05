"""PaperBroker: seeding, capabilities, idempotent close."""
import pytest

from agentic.brokers.factory import build_broker
from agentic.brokers.paper_broker import PaperBroker
from agentic.config import Settings
from agentic.domain.enums import OrderStatus, PositionStatus
from agentic.domain.models import Order


@pytest.mark.asyncio
async def test_paper_buying_power_from_config():
    broker = await build_broker(Settings(broker="paper", paper_buying_power=1500.0))
    assert await broker.get_buying_power() == 1500.0
    assert await broker.get_account_value() == 1500.0


@pytest.mark.asyncio
async def test_seeding_disabled_starts_empty():
    """paper_seed_positions=False -> no demo fixtures, so nothing blocks the entry sizer."""
    broker = await build_broker(
        Settings(broker="paper", paper_buying_power=25_000.0, paper_seed_positions=False)
    )
    assert await broker.get_open_positions() == []


@pytest.mark.asyncio
async def test_seeding_enabled_by_default():
    """Default preserves the demo seed (local dev / existing behavior)."""
    broker = await build_broker(Settings(broker="paper"))
    assert len(await broker.get_open_positions()) == 2


@pytest.mark.asyncio
async def test_seeds_short_positions():
    broker = PaperBroker()
    await broker.connect()
    positions = await broker.get_open_positions()
    assert len(positions) == 2
    assert {p.underlying for p in positions} == {"AAPL", "MSFT"}
    assert all(p.status == PositionStatus.OPEN for p in positions)


def test_capabilities_supports_options_and_is_paper():
    caps = PaperBroker().capabilities()
    assert caps.supports_options_orders is True
    assert caps.is_paper is True


@pytest.mark.asyncio
async def test_close_order_is_idempotent():
    broker = PaperBroker()
    positions = await broker.get_open_positions()
    occ = positions[0].occ_symbol
    order = Order(
        decision_id="d1", position_id=positions[0].id, occ_symbol=occ,
        quantity=1, limit_price=0.75, is_paper=True, client_order_id="fixed-key",
    )
    first = await broker.submit_close_order(order)
    assert first.status == OrderStatus.FILLED
    # Re-submitting the same client_order_id must not create a second fill.
    dup = Order(
        decision_id="d1", position_id=positions[0].id, occ_symbol=occ,
        quantity=1, limit_price=0.75, is_paper=True, client_order_id="fixed-key",
    )
    second = await broker.submit_close_order(dup)
    assert second.broker_order_id == first.broker_order_id
    # Position is now closed and no longer returned as open.
    remaining = await broker.get_open_positions()
    assert occ not in {p.occ_symbol for p in remaining}
