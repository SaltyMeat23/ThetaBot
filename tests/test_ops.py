"""Consolidated /api/ops snapshot + /api/refinement-export join."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agentic.ai.schema import Verdict
from agentic.config import EntryConfig, Settings
from agentic.domain.enums import AuditEventType
from agentic.domain.models import TradeJournalEntry, utcnow
from agentic.services.killswitch import KillSwitch
from agentic.store.ai_reviews import AIReviewStore
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.trade_journal import TradeJournalStore
from agentic.store.tv_indicators import TVIndicatorStore
from agentic.web.app import WebDeps, create_app


def _deps(tmp_path, **over):
    db = Database(tmp_path / "ops.db")
    audit = AuditStore(db)
    base = dict(
        settings=Settings(mode="live", i_understand_live_trading=True,
                          entry=EntryConfig(enabled=True, watchlist=["ONDS"])),
        signals=SignalStore(db), killswitch=KillSwitch(db, audit, auto_trip_threshold=5),
        approval_gate=None, audit=audit, positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), trade_journal=TradeJournalStore(db),
        ai_reviews=AIReviewStore(db), tv_indicators=TVIndicatorStore(db),
    )
    base.update(over)
    return db, audit, base


def test_ops_snapshot(tmp_path):
    db, audit, base = _deps(tmp_path)
    # Loops leave audit heartbeats.
    audit.record(AuditEventType.POLL, {"open_positions": 1}, source="monitor")
    audit.record(AuditEventType.RECONCILE, {"broker_open": 1, "store_open": 1,
                 "entry_decisions_healed": []}, source="reconcile")
    audit.record(AuditEventType.ERROR, {"where": "scanner", "root_causes": ["ValueError: boom"]})
    base["killswitch"].record_broker_error("scanner")  # streak = 1 (threshold 5, not tripped)
    base["tv_indicators"].upsert("ONDS", {"support": 7.5})
    base["scanner"] = SimpleNamespace(last_scan_at=utcnow(), last_error=None, last_skips=[])

    client = TestClient(create_app(WebDeps(**base)))
    ops = client.get("/api/ops").json()

    assert ops["mode"] == "live" and ops["live_armed"] is True and ops["paused"] is False
    assert ops["killswitch"]["consecutive_errors"] == 1
    assert ops["loops"]["monitor_poll"]["lag_seconds"] is not None
    assert ops["sync"] == {"broker_open": 1, "store_open": 1, "in_sync": True, "healed": []}
    assert ops["last_error"]["where"] == "scanner"
    assert ops["last_error"]["root_causes"] == ["ValueError: boom"]
    assert ops["tv_health"]["ok"] is True  # ONDS fresh, watchlist = [ONDS]


def _journal_row(occ, decision_id, status="win", pnl=24.0):
    return TradeJournalEntry(
        occ_symbol=occ, underlying="ONDS", kind="CSP", contracts=1, strike=7.5, dte=7,
        delta=-0.23, iv=0.9, premium=0.2, annualized_ror=108.0, underlying_price=8.1,
        context={"iv_rank": 0.4}, entry_decision_id=decision_id, status=status,
        realized_pnl=pnl,
    )


def test_refinement_export_joins_journal_and_ai(tmp_path):
    db, audit, base = _deps(tmp_path)
    tj: TradeJournalStore = base["trade_journal"]
    ai: AIReviewStore = base["ai_reviews"]
    tj.insert(_journal_row("ONDS260731P00007500", "dec1"))
    ai.insert(occ_symbol="ONDS260731P00007500", underlying="ONDS", decision_id="dec1",
              verdict=Verdict(recommendation="caution", confidence=0.62, rationale="high IV",
                              flags=["extreme_iv"]), move_class="idiosyncratic",
              regime_label="calm", model="claude-opus-4-8")

    client = TestClient(create_app(WebDeps(**base)))
    data = client.get("/api/refinement-export").json()
    assert data["count"] == 1
    row = data["rows"][0]
    assert row["underlying"] == "ONDS" and row["realized_pnl"] == 24.0
    assert row["ai_recommendation"] == "caution" and row["ai_move_class"] == "idiosyncratic"
    assert row["ai_flags"] == ["extreme_iv"] and row["context"] == {"iv_rank": 0.4}

    # CSV form is rectangular with the fixed header.
    csv = client.get("/api/refinement-export?format=csv")
    assert csv.headers["content-type"].startswith("text/csv")
    lines = csv.text.strip().splitlines()
    assert lines[0].startswith("entered_at,closed_at,underlying")
    assert "ONDS" in lines[1] and "caution" in lines[1]


def test_refinement_export_no_ai_review(tmp_path):
    db, audit, base = _deps(tmp_path)
    base["trade_journal"].insert(_journal_row("X260731P00005000", "decX"))
    client = TestClient(create_app(WebDeps(**base)))
    row = client.get("/api/refinement-export").json()["rows"][0]
    assert row["ai_recommendation"] is None  # unreviewed trade still exported
    assert row["ai_flags"] == []
