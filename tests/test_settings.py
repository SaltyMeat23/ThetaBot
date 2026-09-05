"""Stage 2 Increment 1: in-app settings editor — overlay persistence, hot-apply, guardrails."""
import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings, load_config, load_overlay, save_overlay
from agentic.entry.risk import RiskSizer
from agentic.services.approval import ApprovalGate
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.brokers.paper_broker import PaperBroker
from agentic.marketdata.base import PaperMarketData
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app
from agentic.web.settings import SettingsEditError, apply_patch


# --- apply_patch unit tests (the core logic) ---------------------------------------------------

def test_hot_apply_reaches_live_sizer(tmp_path):
    """A sizing edit must reach a RiskSizer that captured settings.entry.sizing by reference."""
    settings = Settings(broker="paper", market_data="paper")
    sizer = RiskSizer(settings.entry.sizing)  # holds the SAME sizing object (as the scanner does)
    ov = tmp_path / "overlay.yaml"

    changed = apply_patch(
        settings, {"entry": {"sizing": {"max_position_size_pct": 0.25}}}, overlay_path=ov
    )

    assert changed == ["entry"]
    assert settings.entry.sizing.max_position_size_pct == 0.25
    # In-place mutation (not replacement) — the running sizer sees the new value:
    assert sizer.sizing.max_position_size_pct == 0.25


def test_edit_persists_and_merges_overlay(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    ov = tmp_path / "overlay.yaml"

    apply_patch(settings, {"paper_buying_power": 30000}, overlay_path=ov)
    apply_patch(settings, {"entry": {"watchlist": ["F", "SOFI"]}}, overlay_path=ov)

    saved = load_overlay(ov)
    assert saved["paper_buying_power"] == 30000
    assert saved["entry"]["watchlist"] == ["F", "SOFI"]


@pytest.mark.parametrize("patch", [
    {"mode": "live"},
    {"i_understand_live_trading": True},
    {"broker": "robinhood_mcp"},
    {"robinhood": {"account_number": "999"}},
    {"market_data": "alpaca"},
])
def test_protected_keys_rejected(tmp_path, patch):
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError):
        apply_patch(settings, patch, overlay_path=tmp_path / "o.yaml")
    # Nothing changed and nothing persisted.
    assert settings.mode == "paper"
    assert not (tmp_path / "o.yaml").exists()


def test_invalid_value_rejected_and_not_applied(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    before = settings.entry.sizing.max_position_size_pct
    with pytest.raises(SettingsEditError):
        apply_patch(
            settings,
            {"entry": {"sizing": {"max_position_size_pct": "not-a-number"}}},
            overlay_path=tmp_path / "o.yaml",
        )
    assert settings.entry.sizing.max_position_size_pct == before


def test_empty_patch_rejected(tmp_path):
    settings = Settings(broker="paper", market_data="paper")
    with pytest.raises(SettingsEditError):
        apply_patch(settings, {}, overlay_path=tmp_path / "o.yaml")


# --- load_config overlay merge -----------------------------------------------------------------

def test_load_config_merges_overlay(tmp_path, monkeypatch):
    base = tmp_path / "config.yaml"
    base.write_text("mode: paper\npaper_buying_power: 1500\nentry:\n  watchlist: [\"AAPL\"]\n")
    ov = tmp_path / "overlay.yaml"
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", ov)
    save_overlay({"paper_buying_power": 25000, "entry": {"watchlist": ["F", "SOFI"]}}, ov)

    s = load_config(base)
    assert s.paper_buying_power == 25000
    assert s.entry.watchlist == ["F", "SOFI"]
    # Base value not touched by the overlay stays put.
    assert s.mode == "paper"


# --- endpoint tests ----------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.config.OVERLAY_PATH", tmp_path / "overlay.yaml")
    db = Database(tmp_path / "s.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    orders = OrderStore(db)
    signals = SignalStore(db)
    killswitch = KillSwitch(db, audit)
    settings = Settings(mode="paper", broker="paper", market_data="paper")
    broker = PaperBroker(seed_positions=[])
    executor = OrderExecutor(settings, broker, PaperMarketData(), positions, orders,
                             decisions, audit, killswitch, poll_interval_seconds=0.001)
    approval_gate = ApprovalGate(settings, decisions, positions, executor, audit)
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch,
                   approval_gate=approval_gate, audit=audit,
                   positions=positions, orders=orders, decisions=decisions)
    return TestClient(create_app(deps)), settings


def test_get_config_returns_editable_and_readonly(client):
    c, _ = client
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert "entry" in body["editable"]
    assert body["readonly"]["mode"] == "paper"
    assert body["readonly"]["live_armed"] is False
    # The dangerous knobs are reported read-only, never in the editable set.
    assert "mode" not in body["editable"]


def test_post_valid_edit_applies(client):
    c, settings = client
    r = c.post("/api/config", json={"entry": {"watchlist": ["F", "SOFI", "T"]}})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert settings.entry.watchlist == ["F", "SOFI", "T"]


def test_post_protected_edit_rejected(client):
    c, settings = client
    r = c.post("/api/config", json={"mode": "live"})
    assert r.status_code == 400
    assert r.json()["ok"] is False
    assert settings.mode == "paper"
