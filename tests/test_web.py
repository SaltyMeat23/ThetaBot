"""Phase 3 end-to-end: webhook auth/dedup + signal -> approval -> execute via the API."""
import pytest
from fastapi.testclient import TestClient

from agentic.brokers import paper_broker as pb
from agentic.brokers.paper_broker import PaperBroker
from agentic.config import Settings
from agentic.domain.enums import DecisionStatus, PositionStatus
from agentic.marketdata.base import PaperMarketData
from agentic.services.approval import ApprovalGate
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.signal_processor import SignalProcessor
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

TOKEN = "test-secret-token"
CONTROL = "test-control-token"


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADINGVIEW_WEBHOOK_TOKEN", TOKEN)
    monkeypatch.setenv("CONTROL_TOKEN", CONTROL)
    db = Database(tmp_path / "web.db")
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    entry_decisions = EntryDecisionStore(db)
    orders = OrderStore(db)
    signals = SignalStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker()
    settings = Settings(mode="paper", broker="paper", market_data="paper")

    # One open AAPL position, present in both the store and the (paper) broker.
    pos = pb._default_seed()[0]
    positions.upsert(pos)

    executor = OrderExecutor(settings, broker, PaperMarketData(), positions, orders,
                             decisions, audit, killswitch, poll_interval_seconds=0.001)
    approval_gate = ApprovalGate(settings, decisions, positions, executor, audit)
    signal_processor = SignalProcessor(settings, signals, positions, decisions, executor,
                                       approval_gate, audit)
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch,
                   approval_gate=approval_gate, audit=audit,
                   positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions)
    client = TestClient(create_app(deps))
    return dict(client=client, signals=signals, decisions=decisions, positions=positions,
                processor=signal_processor, occ=pos.occ_symbol, killswitch=killswitch,
                entry_decisions=entry_decisions)


def test_health(ctx):
    r = ctx["client"].get("/health")
    assert r.status_code == 200
    assert r.json()["mode"] == "paper"


def test_webhook_rejects_bad_token(ctx):
    r = ctx["client"].post("/webhook/tradingview", json={"token": "wrong", "symbol": "AAPL"})
    assert r.status_code == 401
    from agentic.domain.enums import SignalStatus
    assert ctx["signals"].list_by_status(SignalStatus.NEW) == []


def test_webhook_queues_then_dedupes(ctx):
    body = {"token": TOKEN, "action": "close", "symbol": "AAPL", "alert_id": "abc-1"}
    r1 = ctx["client"].post("/webhook/tradingview", json=body)
    assert r1.status_code == 202
    assert r1.json()["status"] == "queued"
    # Same alert_id -> duplicate, no second signal.
    r2 = ctx["client"].post("/webhook/tradingview", json=body)
    assert r2.status_code == 200
    assert r2.json()["status"] == "duplicate"
    from agentic.domain.enums import SignalStatus
    assert len(ctx["signals"].list_by_status(SignalStatus.NEW)) == 1


@pytest.mark.asyncio
async def test_signal_to_approval_to_execution(ctx):
    client = ctx["client"]
    # 1. Webhook enqueues a close signal for AAPL.
    r = client.post("/webhook/tradingview",
                    json={"token": TOKEN, "action": "close", "symbol": "AAPL", "alert_id": "x1"})
    assert r.status_code == 202

    # 2. Monitor would call this each cycle; do it directly.
    await ctx["processor"].process_pending()

    # 3. A decision is now AWAITING_APPROVAL (signals default to approval-gated).
    pending = ctx["decisions"].list_by_status(DecisionStatus.AWAITING_APPROVAL)
    assert len(pending) == 1
    decision_id = pending[0].id

    # 4. Approve via the control endpoint -> executes the paper close.
    from agentic.config import close_action_token
    tok = close_action_token(decision_id)
    assert client.post(f"/control/approve/{decision_id}").status_code == 401  # no token -> refused
    ra = client.post(f"/control/approve/{decision_id}?t={tok}")
    assert ra.status_code == 200
    assert ra.json()["status"] == "approved"

    # 5. Position is closed and the decision is DONE.
    assert ctx["positions"].get_by_occ(ctx["occ"]).status == PositionStatus.CLOSED
    assert ctx["decisions"].get(decision_id).status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_reject_blocks_execution(ctx):
    client = ctx["client"]
    client.post("/webhook/tradingview",
                json={"token": TOKEN, "action": "close", "symbol": "AAPL", "alert_id": "x2"})
    await ctx["processor"].process_pending()
    decision_id = ctx["decisions"].list_by_status(DecisionStatus.AWAITING_APPROVAL)[0].id

    from agentic.config import close_action_token
    rr = client.post(f"/control/reject/{decision_id}?t={close_action_token(decision_id)}")
    assert rr.status_code == 200
    assert ctx["decisions"].get(decision_id).status == DecisionStatus.REJECTED
    assert ctx["positions"].get_by_occ(ctx["occ"]).status == PositionStatus.OPEN


def _seed_failed_entry(ctx, occ="ONDS260731P00007500"):
    from datetime import date
    from agentic.domain.models import EntryDecision
    d = EntryDecision(
        underlying="ONDS", occ_symbol=occ, option_id=None, strike=7.5,
        expiration=date(2026, 7, 31), contracts=1, premium=0.2, rule_name="csp-screener",
        reason="CSP", dedup_key=f"{occ}:heal-test",
    )
    ctx["entry_decisions"].insert_if_new(d)
    ctx["entry_decisions"].set_status(d.id, DecisionStatus.FAILED)
    return d


def test_heal_decision_flips_failed_to_done(ctx):
    d = _seed_failed_entry(ctx)
    client = ctx["client"]
    # No token -> refused (CONTROL_TOKEN is set in the fixture env).
    assert client.post(f"/control/heal-decision/{d.id}").status_code == 401
    r = client.post(f"/control/heal-decision/{d.id}?token={CONTROL}")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "occ": "ONDS260731P00007500", "from": "FAILED", "to": "DONE"}
    assert ctx["entry_decisions"].get(d.id).status == DecisionStatus.DONE


def test_heal_decision_custom_status(ctx):
    d = _seed_failed_entry(ctx, occ="X260731P00005000")
    r = ctx["client"].post(f"/control/heal-decision/{d.id}?status=expired&token={CONTROL}")
    assert r.status_code == 200 and r.json()["to"] == "EXPIRED"
    assert ctx["entry_decisions"].get(d.id).status == DecisionStatus.EXPIRED


def test_heal_decision_unknown_and_invalid(ctx):
    client = ctx["client"]
    assert client.post(f"/control/heal-decision/nope?token={CONTROL}").status_code == 404
    d = _seed_failed_entry(ctx, occ="Y260731P00003000")
    bad = client.post(f"/control/heal-decision/{d.id}?status=BOGUS&token={CONTROL}")
    assert bad.status_code == 400 and "valid" in bad.json()


def test_mcp_tools_diagnostic(tmp_path):
    """The diagnostic surfaces the broker's cached MCP tool list (to see new RH capabilities)."""
    from types import SimpleNamespace
    from agentic.brokers.base import BrokerCapabilities
    db = Database(tmp_path / "mcp.db")
    audit = AuditStore(db)
    broker = SimpleNamespace(
        _tools=["get_option_chains", "get_technical_indicators", "get_earnings"],
        _tool_defs={"get_earnings": {"description": "Earnings", "input_schema": {"type": "object"}}},
        _roles={"option_chain": "get_option_chains"},
        capabilities=lambda: BrokerCapabilities("robinhood_mcp", supports_options_orders=True,
                                                is_paper=False),
    )
    client = TestClient(create_app(WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db), killswitch=KillSwitch(db, audit),
        approval_gate=None, audit=audit, positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), scanner=SimpleNamespace(broker=broker),
    )))
    r = client.get("/control/mcp-tools").json()
    assert r["ok"] and r["tool_count"] == 3
    assert "get_technical_indicators" in r["tools"] and "get_earnings" in r["tools"]
    # ?tool= returns the full schema for planning wiring.
    sch = client.get("/control/mcp-tools?tool=get_earnings,nope").json()["schemas"]
    assert sch["get_earnings"]["input_schema"] == {"type": "object"}
    assert "error" in sch["nope"]


def test_pause_resume(ctx):
    client = ctx["client"]
    assert client.post("/control/pause").status_code == 401             # CONTROL_TOKEN now required
    assert client.post(f"/control/pause?token={CONTROL}").json()["status"] == "paused"
    assert ctx["killswitch"].is_paused() is True
    assert client.post(f"/control/resume?token={CONTROL}").json()["status"] == "resumed"
    assert ctx["killswitch"].is_paused() is False
