"""TradingView indicator channel: store upsert/TTL + webhook routing (non-breaking on closes)."""
import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.domain.enums import SignalStatus
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.tv_indicators import TVIndicatorStore
from agentic.services.killswitch import KillSwitch
from agentic.web.app import WebDeps, create_app

TOKEN = "tv-secret"


def test_upsert_and_get_latest(tmp_path):
    db = Database(tmp_path / "tv.db")
    store = TVIndicatorStore(db)
    store.upsert("f", {"rsi": 41, "trend": "up"})
    latest = store.get_latest("F")
    assert latest is not None
    assert latest["symbol"] == "F"
    assert latest["payload"]["rsi"] == 41
    # Re-upsert MERGES: a repeated key overwrites, untouched keys persist.
    store.upsert("F", {"rsi": 55})
    assert store.get_latest("F")["payload"] == {"rsi": 55, "trend": "up"}


def test_upsert_merges_multiple_studies(tmp_path):
    """The real case: a support/resistance alert + a separate features alert accumulate into one
    vector per symbol instead of clobbering each other."""
    store = TVIndicatorStore(Database(tmp_path / "tv.db"))
    store.upsert("SOFI", {"support": 17.9, "resistance": 18.4, "tf": "30"})   # S/R alert
    store.upsert("SOFI", {"adx": 22.5, "bb_percent_b": 61.0})                 # features alert
    payload = store.get_latest("SOFI")["payload"]
    assert payload["support"] == 17.9 and payload["resistance"] == 18.4       # S/R survived
    assert payload["adx"] == 22.5 and payload["bb_percent_b"] == 61.0         # features added
    store.upsert("SOFI", {"support": 18.1})                                   # S/R refresh
    payload = store.get_latest("SOFI")["payload"]
    assert payload["support"] == 18.1 and payload["adx"] == 22.5              # refresh keeps adx


def test_get_latest_absent_returns_none(tmp_path):
    store = TVIndicatorStore(Database(tmp_path / "tv.db"))
    assert store.get_latest("NOPE") is None


def test_get_latest_respects_ttl(tmp_path):
    store = TVIndicatorStore(Database(tmp_path / "tv.db"))
    store.upsert("F", {"rsi": 41})
    # A zero-second freshness window makes any stored row already stale.
    assert store.get_latest("F", max_age_seconds=0) is None
    assert store.get_latest("F", max_age_seconds=3600) is not None


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_TOKEN", TOKEN)
    db = Database(tmp_path / "w.db")
    audit = AuditStore(db)
    signals = SignalStore(db)
    tv = TVIndicatorStore(db)
    deps = WebDeps(
        settings=Settings(broker="paper", market_data="paper"),
        signals=signals, killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        tv_indicators=tv,
    )
    return TestClient(create_app(deps)), signals, tv


def test_indicator_alert_is_stored_not_queued(client):
    c, signals, tv = client
    r = c.post("/webhook/tradingview",
               json={"token": TOKEN, "action": "indicator", "symbol": "F", "rsi": 38})
    assert r.status_code == 202
    assert r.json()["status"] == "indicator_stored"
    # It went to the indicator store, NOT the close-signal queue.
    assert tv.get_latest("F")["payload"]["rsi"] == 38
    assert signals.list_by_status(SignalStatus.NEW) == []


def test_indicator_alert_requires_symbol(client):
    c, _, _ = client
    r = c.post("/webhook/tradingview", json={"token": TOKEN, "action": "indicator"})
    assert r.status_code == 400


def test_close_signal_still_queued(client):
    c, signals, tv = client
    r = c.post("/webhook/tradingview",
               json={"token": TOKEN, "action": "close", "symbol": "AAPL", "alert_id": "x1"})
    assert r.status_code == 202
    assert len(signals.list_by_status(SignalStatus.NEW)) == 1
    assert tv.get_latest("AAPL") is None


def test_recent_returns_stored(tmp_path):
    store = TVIndicatorStore(Database(tmp_path / "r.db"))
    store.upsert("SOFI", {"support": 16.5, "resistance": 19.1})
    store.upsert("F", {"support": 12.8, "trend": "up"})
    rows = store.recent()
    syms = {r["symbol"] for r in rows}
    assert syms == {"SOFI", "F"}
    sofi = next(r for r in rows if r["symbol"] == "SOFI")
    assert sofi["payload"]["support"] == 16.5 and sofi["payload"]["resistance"] == 19.1


def test_indicator_token_via_url_query_and_secret_stripped(client):
    c, signals, tv = client
    r = c.post("/webhook/tradingview?t=" + TOKEN,
               json={"action": "indicator", "symbol": "SOFI", "support": 16.5, "resistance": 19.1})
    assert r.status_code == 202                         # token accepted from the URL, no body token
    stored = tv.get_latest("SOFI")["payload"]
    assert "token" not in stored                        # secret never persisted/echoed
    assert stored["support"] == 16.5 and stored["resistance"] == 19.1
