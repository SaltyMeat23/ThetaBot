"""TV-indicator freshness health: classification, digest line, endpoint."""
from fastapi.testclient import TestClient

from agentic.config import EntryConfig, Settings
from agentic.services.killswitch import KillSwitch
from agentic.services.reporting import build_daily_digest
from agentic.services.tv_health import build_tv_health, tv_health_digest_line
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.tv_indicators import TVIndicatorStore
from agentic.web.app import WebDeps, create_app

MAX_AGE = 108_000  # 30h


class FakeTV:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, limit=500):
        return self._rows


def _row(sym, age, ts, sup=None, res=None):
    return {"symbol": sym, "age_seconds": age, "received_at": ts,
            "payload": {"support": sup, "resistance": res}}


def test_build_tv_health_classifies():
    store = FakeTV([
        _row("ONDS", 200_000.0, "2026-07-22T20:01:00+00:00", 7.78, 8.32),  # present but stale
        _row("BULL", 100.0, "2026-07-25T15:00:00+00:00", 7.66, 10.18),      # present + fresh
        _row("OLDNAME", 50.0, "2026-07-25T15:30:00+00:00"),                 # cruft; newest overall
    ])
    h = build_tv_health(store, ["ONDS", "BULL", "NOPE"], MAX_AGE)
    assert h["stale"] == ["ONDS"]
    assert h["missing"] == ["NOPE"]
    assert h["fresh_count"] == 1
    assert h["cruft"] == ["OLDNAME"]
    assert h["ok"] is False
    # New: per-symbol levels + received_at, and the "last webhook overall".
    bull = next(s for s in h["symbols"] if s["symbol"] == "BULL")
    assert bull["support"] == 7.66 and bull["resistance"] == 10.18
    assert bull["received_at"] == "2026-07-25T15:00:00+00:00"
    nope = next(s for s in h["symbols"] if s["symbol"] == "NOPE")
    assert nope["present"] is False and nope["support"] is None
    assert h["latest"]["symbol"] == "OLDNAME"   # newest received_at across all stored


def test_digest_line_only_when_problems():
    ok = {"missing": [], "stale": [], "threshold_seconds": MAX_AGE}
    assert tv_health_digest_line(ok) is None
    bad = {"missing": ["NOPE"], "stale": ["ONDS"], "threshold_seconds": MAX_AGE}
    line = tv_health_digest_line(bad)
    assert "ONDS" in line and "NOPE" in line and "30h" in line


def test_daily_digest_appends_tv_warning():
    _title, msg = build_daily_digest(
        stats={}, rows=[], scanner=None, mode="live",
        tv_health={"missing": [], "stale": ["ONDS"], "threshold_seconds": MAX_AGE},
    )
    assert "TV indicators stale" in msg and "ONDS" in msg


def test_tv_health_endpoint(tmp_path):
    db = Database(tmp_path / "tv.db")
    audit = AuditStore(db)
    tv = TVIndicatorStore(db)
    tv.upsert("ONDS", {"support": 7.5, "resistance": 8.3})  # fresh
    deps = WebDeps(
        settings=Settings(mode="paper", entry=EntryConfig(watchlist=["ONDS", "NOPE"])),
        signals=SignalStore(db), killswitch=KillSwitch(db, audit), approval_gate=None,
        audit=audit, positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), tv_indicators=tv,
    )
    client = TestClient(create_app(deps))
    h = client.get("/api/tv-health").json()
    assert h["missing"] == ["NOPE"]
    onds = next(s for s in h["symbols"] if s["symbol"] == "ONDS")
    assert onds["present"] is True and onds["stale"] is False
