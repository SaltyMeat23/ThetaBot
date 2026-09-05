"""CloseDecision repository with dedup-on-insert.

A decision's ``dedup_key`` (e.g. ``<position_id>:<rule_type>:<date>``) has a UNIQUE
index, so ``insert_if_new`` returns False when the same condition was already recorded —
this prevents the monitor from re-deciding (and re-notifying) the same thing every cycle.
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import DecisionStatus, RuleType
from ..domain.models import CloseDecision
from .db import Database


def _row_to_decision(r) -> CloseDecision:
    return CloseDecision(
        id=r["id"],
        position_id=r["position_id"],
        rule_name=r["rule_name"],
        rule_type=RuleType(r["rule_type"]),
        reason=r["reason"],
        requires_approval=bool(r["requires_approval"]),
        dedup_key=r["dedup_key"],
        status=DecisionStatus(r["status"]),
        created_at=datetime.fromisoformat(r["created_at"]),
        decided_at=datetime.fromisoformat(r["decided_at"]) if r["decided_at"] else None,
        expires_at=datetime.fromisoformat(r["expires_at"]) if r["expires_at"] else None,
    )


class DecisionStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, d: CloseDecision) -> bool:
        """Insert the decision. Returns True if newly inserted, False if a row with the
        same dedup_key already exists."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO decisions
                 (id, position_id, rule_name, rule_type, reason, requires_approval,
                  status, dedup_key, created_at, decided_at, expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.id, d.position_id, d.rule_name, d.rule_type.value, d.reason,
                1 if d.requires_approval else 0, d.status.value, d.dedup_key,
                d.created_at.isoformat(),
                d.decided_at.isoformat() if d.decided_at else None,
                d.expires_at.isoformat() if d.expires_at else None,
            ),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def set_status(self, decision_id: str, status: DecisionStatus) -> None:
        self.db.conn.execute(
            "UPDATE decisions SET status = ?, decided_at = ? WHERE id = ?",
            (status.value, datetime.now().astimezone().isoformat(), decision_id),
        )
        self.db.conn.commit()

    def mark_awaiting(self, decision_id: str, expires_at: datetime) -> None:
        """Move a decision to AWAITING_APPROVAL with an expiry (decided_at stays null until
        the human responds)."""
        self.db.conn.execute(
            "UPDATE decisions SET status = ?, expires_at = ? WHERE id = ?",
            (DecisionStatus.AWAITING_APPROVAL.value, expires_at.isoformat(), decision_id),
        )
        self.db.conn.commit()

    def get(self, decision_id: str) -> CloseDecision | None:
        r = self.db.conn.execute(
            "SELECT * FROM decisions WHERE id = ?", (decision_id,)
        ).fetchone()
        return _row_to_decision(r) if r else None

    def list_by_status(self, status: DecisionStatus) -> list[CloseDecision]:
        rows = self.db.conn.execute(
            "SELECT * FROM decisions WHERE status = ? ORDER BY created_at", (status.value,)
        ).fetchall()
        return [_row_to_decision(r) for r in rows]

    def recent(self, limit: int = 100) -> list[CloseDecision]:
        """Most recent decisions first — the 'why' log for the dashboard."""
        rows = self.db.conn.execute(
            "SELECT * FROM decisions ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_decision(r) for r in rows]
