"""Dashboard: compute_stats P&L/win-rate logic + read-only API endpoints."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.domain.enums import (
    DecisionStatus, OptionType, OrderStatus, PositionStatus, RuleType, Strategy,
)
from agentic.domain.models import CloseDecision, Order, Position
from agentic.services.killswitch import KillSwitch
from agentic.services.stats import compute_stats, position_pnl
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app


def _pos(occ, status, credit, qty=1, mark=None, opt=OptionType.CALL):
    return Position(
        occ_symbol=occ, underlying="AAPL", option_type=opt,
        strategy=Strategy.COVERED_CALL if opt is OptionType.CALL else Strategy.CASH_SECURED_PUT,
        quantity=qty, strike=250.0, expiration=date.today() + timedelta(days=10),
        credit_received=credit, current_mark=mark, status=status,
    )


def _filled(position_id, avg):
    return Order(
        decision_id="d", position_id=position_id, occ_symbol="X", quantity=1,
        limit_price=avg, is_paper=True, status=OrderStatus.FILLED, avg_fill_price=avg,
    )


def _decision(position_id, rule_name, key):
    return CloseDecision(
        position_id=position_id, rule_name=rule_name, rule_type=RuleType.PROFIT_TARGET,
        reason="r", requires_approval=False, dedup_key=key, status=DecisionStatus.DONE,
    )


def test_position_pnl_outcomes():
    # short expired worthless -> keep full credit (win)
    assert position_pnl(_pos("A", PositionStatus.EXPIRED, 1.0), None)["realized_pnl"] == 100.0
    # closed for a profit
    p = _pos("B", PositionStatus.CLOSED, 2.0)
    info = position_pnl(p, _filled(p.id, 0.5))
    assert info["outcome"] == "win" and info["realized_pnl"] == 150.0
    # closed for a loss
    p = _pos("C", PositionStatus.CLOSED, 1.0)
    info = position_pnl(p, _filled(p.id, 1.8))
    assert info["outcome"] == "loss" and info["realized_pnl"] == -80.0
    # open with a mark -> unrealized only
    info = position_pnl(_pos("D", PositionStatus.OPEN, 1.5, qty=2, mark=1.0), None)
    assert info["outcome"] == "open" and info["realized_pnl"] is None
    assert info["unrealized_pnl"] == 100.0
    # assigned -> excluded from P&L
    assert position_pnl(_pos("E", PositionStatus.ASSIGNED, 1.0), None)["outcome"] == "assigned"


def test_compute_stats_aggregates():
    p_exp = _pos("EXP", PositionStatus.EXPIRED, 1.0)
    p_win = _pos("WIN", PositionStatus.CLOSED, 2.0)
    p_loss = _pos("LOSS", PositionStatus.CLOSED, 1.0)
    p_open = _pos("OPEN", PositionStatus.OPEN, 1.5, qty=2, mark=1.0)
    p_asn = _pos("ASN", PositionStatus.ASSIGNED, 1.0)
    positions = [p_exp, p_win, p_loss, p_open, p_asn]
    orders = [_filled(p_win.id, 0.5), _filled(p_loss.id, 1.8)]
    decisions = [_decision(p_win.id, "profit-50", "k1"), _decision(p_loss.id, "dte-21", "k2")]

    s = compute_stats(positions, orders, decisions)
    assert s["wins"] == 2 and s["losses"] == 1 and s["assigned"] == 1 and s["open_count"] == 1
    assert s["win_rate"] == 2 / 3
    assert s["realized_pnl"] == 170.0      # +100 +150 -80
    assert s["unrealized_pnl"] == 100.0
    rules = {r["rule"]: r for r in s["by_rule"]}
    assert rules["profit-50"]["realized_pnl"] == 150.0 and rules["profit-50"]["wins"] == 1
    assert rules["dte-21"]["realized_pnl"] == -80.0 and rules["dte-21"]["wins"] == 0
    assert rules["expiry/other"]["realized_pnl"] == 100.0   # expired position, no decision


def test_dashboard_endpoints(tmp_path):
    db = Database(tmp_path / "dash.db")
    audit, positions, decisions, orders = (
        AuditStore(db), PositionStore(db), DecisionStore(db), OrderStore(db)
    )
    p = _pos("AAPL260622C00250000", PositionStatus.CLOSED, 2.0)
    positions.upsert(p)
    o = _filled(p.id, 0.5)
    orders.insert_if_new(o)
    decisions.insert_if_new(_decision(p.id, "profit-50", "dk1"))

    deps = WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=positions, orders=orders, decisions=decisions,
    )
    client = TestClient(create_app(deps))

    page = client.get("/dashboard")
    assert page.status_code == 200 and "AgenticRobinhood" in page.text

    stats = client.get("/api/stats").json()
    assert stats["wins"] == 1 and stats["realized_pnl"] == 150.0

    rows = client.get("/api/positions").json()["positions"]
    assert rows[0]["outcome"] == "win" and rows[0]["rule"] == "profit-50"

    decs = client.get("/api/decisions").json()["decisions"]
    assert decs[0]["rule_name"] == "profit-50"


def _bare_app(tmp_path):
    db = Database(tmp_path / "auth.db")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db),
        killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
    )
    return TestClient(create_app(deps))


def test_dashboard_ships_control_ui(tmp_path):
    """The redesigned page must include the connection hero + the editable controls."""
    page = _bare_app(tmp_path).get("/dashboard").text
    for marker in ('id="wl-in"', 'id="wk-in"', "What you&#39;re holding".replace("&#39;", "'"),
                   "Weekly premium target", "Connected to Robinhood", "TradingView levels",
                   'id="tv-levels"', "powered by AgenticRobinhood"):
        assert marker in page, f"missing dashboard marker: {marker!r}"


def test_auth_open_when_no_password(tmp_path, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    client = _bare_app(tmp_path)
    assert client.get("/api/stats").status_code == 200       # open in dev
    assert client.get("/dashboard").status_code == 200
    assert client.get("/health").status_code == 200


def test_auth_enforced_when_password_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DASHBOARD_USER", "me")
    monkeypatch.setenv("DASHBOARD_PASSWORD", "s3cret")
    client = _bare_app(tmp_path)
    # Protected endpoints reject anonymous / wrong creds...
    assert client.get("/api/stats").status_code == 401
    assert client.get("/dashboard").status_code == 401
    assert client.get("/control/status").status_code == 401
    assert client.get("/api/stats", auth=("me", "nope")).status_code == 401
    # ...and accept correct creds.
    assert client.get("/api/stats", auth=("me", "s3cret")).status_code == 200
    assert client.get("/dashboard", auth=("me", "s3cret")).status_code == 200
    # Open endpoints stay open: health (Coolify healthcheck) is never auth-gated.
    assert client.get("/health").status_code == 200
