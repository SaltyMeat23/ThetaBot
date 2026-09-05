"""Phase 0 end-to-end (paper): monitor sync + reconcile discovery/closed-detection."""
import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import Settings
from agentic.domain.enums import PositionStatus
from agentic.marketdata.base import PaperMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.services.reconcile import ReconcileLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.positions import PositionStore


@pytest.fixture()
def wiring(tmp_path):
    db = Database(tmp_path / "test.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    return db, audit, positions, killswitch, broker, settings


@pytest.mark.asyncio
async def test_monitor_syncs_positions_into_store(wiring):
    db, audit, positions, killswitch, broker, settings = wiring
    monitor = MonitorLoop(settings, broker, PaperMarketData(), positions, audit, killswitch)
    seen = await monitor.run_once()
    assert seen == 2
    stored = positions.list_open()
    assert len(stored) == 2
    # Quote enrichment populated marks.
    assert all(p.current_mark is not None for p in stored)
    # A POLL audit event was recorded.
    assert any(e["event_type"] == "POLL" for e in audit.recent())


@pytest.mark.asyncio
async def test_reconcile_discovers_then_detects_external_close(wiring):
    db, audit, positions, killswitch, broker, settings = wiring
    reconcile = ReconcileLoop(settings, broker, positions, audit)

    diff = await reconcile.run_once()
    assert len(diff["discovered"]) == 2
    assert len(positions.list_open()) == 2

    # Simulate the user closing one position outside the system.
    broker._positions.popitem()
    diff2 = await reconcile.run_once()
    assert len(diff2["gone"]) == 1
    # The gone position (still pre-expiration) is classified CLOSED; one remains open.
    assert diff2["gone"][0]["status"] == PositionStatus.CLOSED.value
    statuses = [p.status for p in positions.list_open()]
    assert PositionStatus.CLOSED not in statuses
    assert len(statuses) == 1


def test_killswitch_persists(wiring):
    db, audit, positions, killswitch, broker, settings = wiring
    assert killswitch.is_paused() is False
    killswitch.pause("test")
    assert killswitch.is_paused() is True
    # New KillSwitch on same DB sees persisted state.
    assert KillSwitch(db, audit).is_paused() is True
    killswitch.resume("test")
    assert killswitch.is_paused() is False
