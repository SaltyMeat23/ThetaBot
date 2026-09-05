"""Scheduled reporting: send-timing logic, digest/weekly builders, and /control/test-notify."""
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from agentic.config import Settings
from agentic.services.reporting import (
    build_daily_digest,
    build_weekly_report,
    should_send_daily,
    should_send_weekly,
)
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app


# --- scheduling logic ---------------------------------------------------------------------------

def test_daily_sends_once_after_time():
    cur = datetime(2026, 7, 6, 16, 20)  # 16:20
    assert should_send_daily(cur, None, 16, 15) is True          # past 16:15, not sent today
    assert should_send_daily(cur, date(2026, 7, 6), 16, 15) is False  # already sent today


def test_daily_not_before_time():
    cur = datetime(2026, 7, 6, 9, 45)
    assert should_send_daily(cur, None, 16, 15) is False


def test_weekly_only_on_weekday_after_time():
    fri = datetime(2026, 7, 3, 16, 30)   # Friday
    thu = datetime(2026, 7, 2, 16, 30)   # Thursday
    assert should_send_weekly(fri, None, 4, 16, 15) is True
    assert should_send_weekly(thu, None, 4, 16, 15) is False
    assert should_send_weekly(fri, date(2026, 7, 3), 4, 16, 15) is False  # already sent


# --- builders -----------------------------------------------------------------------------------

class _Scan:
    last_scan_at = datetime(2026, 7, 6, 15, 55)
    last_candidates = [1, 2, 3]
    last_skips = [{"symbol": "XLF", "reason": "spread"}]
    last_error = None


def test_daily_digest_summarizes():
    stats = {"open_count": 1, "resolved_count": 2, "wins": 2, "losses": 0, "win_rate": 1.0,
             "realized_pnl": 140.0, "unrealized_pnl": -12.0}
    rows = [{"status": "OPEN", "underlying": "F", "strike": 11.0, "option_type": "put",
             "quantity": 1, "dte": 33, "unrealized_pnl": -12.0},
            {"status": "OPEN", "underlying": "ONDS", "strike": 7.5, "option_type": "put",
             "quantity": 1, "dte": 9, "unrealized_pnl": -6.0}]
    title, msg = build_daily_digest(stats=stats, rows=rows, scanner=_Scan(), mode="paper")
    assert "open" in title
    assert "F 11P" in msg and "3 candidates" in msg and "100% win" in msg
    assert "ONDS 7.5P" in msg and "ONDS 8P" not in msg   # fractional strike, not rounded to 8


def test_weekly_report_summarizes():
    stats = {"resolved_count": 5, "wins": 4, "losses": 1, "assigned": 0, "win_rate": 0.8,
             "realized_pnl": 260.0, "unrealized_pnl": 5.0, "credit_collected_resolved": 300.0,
             "by_rule": [{"rule": "profit-50", "closes": 4, "wins": 4, "realized_pnl": 260.0}]}
    title, msg = build_weekly_report(stats=stats, rows=[])
    assert title == "Bot weekly report"
    assert "80% win" in msg and "profit-50" in msg
    assert "This week (last 7d)" in msg          # windowed, not cumulative


def test_weekly_report_shows_cumulative_and_ai_summary():
    week = {"resolved_count": 2, "wins": 2, "losses": 0, "assigned": 0, "win_rate": 1.0,
            "realized_pnl": 40.0, "unrealized_pnl": -16.0, "credit_collected_resolved": 50.0,
            "by_rule": [{"rule": "profit-trail", "closes": 2, "wins": 2, "realized_pnl": 40.0}]}
    cumulative = {"realized_pnl": 260.0, "resolved_count": 12, "win_rate": 0.83}
    _, msg = build_weekly_report(stats=week, rows=[], cumulative=cumulative,
                                 ai_summary="Steady week. Watch SOFI into expiry.")
    assert "Since inception: $260 realized · 12 trades · 83% win" in msg
    assert "Watch SOFI into expiry." in msg


# --- /control/test-notify -----------------------------------------------------------------------

class _FakeNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, priority="normal", actions=None):
        self.sent.append((title, message))


@pytest.fixture()
def ctx(tmp_path):
    db = Database(tmp_path / "r.db")
    audit = AuditStore(db)
    notifier = _FakeNotifier()
    deps = WebDeps(
        settings=Settings(broker="paper", market_data="paper"),
        signals=SignalStore(db), killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        notifier=notifier,
    )
    return TestClient(create_app(deps)), notifier


def test_test_notify_sends(ctx):
    client, notifier = ctx
    r = client.post("/control/test-notify")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(notifier.sent) == 1


def test_test_notify_without_notifier(tmp_path):
    db = Database(tmp_path / "r2.db")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(broker="paper", market_data="paper"),
        signals=SignalStore(db), killswitch=KillSwitch(db, audit), approval_gate=None, audit=audit,
        positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
        notifier=None,
    )
    r = TestClient(create_app(deps)).post("/control/test-notify")
    assert r.status_code == 400
