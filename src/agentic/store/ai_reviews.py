"""Persistence for AI trade-analyst verdicts (one row per reviewed candidate) — the dashboard's
"why" surface. Advisory records: they never gate a trade by themselves."""
from __future__ import annotations

import json
import uuid

from ..ai.schema import Verdict
from ..domain.models import utcnow
from .db import Database


class AIReviewStore:
    def __init__(self, db: Database):
        self.db = db

    def insert(
        self,
        *,
        occ_symbol: str,
        underlying: str,
        verdict: Verdict,
        decision_id: str | None = None,
        regime_label: str | None = None,
        move_class: str | None = None,
        model: str | None = None,
    ) -> None:
        self.db.conn.execute(
            """INSERT INTO ai_reviews
                 (id, decision_id, occ_symbol, underlying, recommendation, confidence, rationale,
                  flags, regime_label, move_class, model, verdict, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex, decision_id, occ_symbol, underlying,
                verdict.recommendation, verdict.confidence, verdict.rationale,
                json.dumps(verdict.flags), regime_label, move_class, model,
                json.dumps(verdict.as_dict()), utcnow().isoformat(),
            ),
        )
        self.db.conn.commit()

    def by_decision(self) -> dict[str, dict]:
        """Map entry-decision id -> its AI review (newest wins) for joining against the journal.

        Rows are inserted newest-last per decision; iterating ascending lets the latest overwrite.
        """
        rows = self.db.conn.execute(
            "SELECT * FROM ai_reviews WHERE decision_id IS NOT NULL ORDER BY created_at ASC"
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            out[r["decision_id"]] = {
                "ai_recommendation": r["recommendation"], "ai_confidence": r["confidence"],
                "ai_flags": json.loads(r["flags"] or "[]"), "ai_regime_label": r["regime_label"],
                "ai_move_class": r["move_class"], "ai_model": r["model"],
                "ai_rationale": r["rationale"],
            }
        return out

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.conn.execute(
            "SELECT * FROM ai_reviews ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            out.append({
                "occ_symbol": r["occ_symbol"], "underlying": r["underlying"],
                "recommendation": r["recommendation"], "confidence": r["confidence"],
                "rationale": r["rationale"], "flags": json.loads(r["flags"] or "[]"),
                "regime_label": r["regime_label"], "move_class": r["move_class"],
                "model": r["model"], "created_at": r["created_at"],
            })
        return out
