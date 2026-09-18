"""Brief archive -- every generated weekly tactical brief, kept so it can be re-read later.

The brief is expensive (live account reads + an optional Claude synthesis) and the operator is
usually away from a keyboard, so each generation is persisted and the dashboard opens on the most
recent saved copy instead of regenerating. Rows are small (a few KB of markdown); ``prune`` keeps
the archive bounded.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..domain.models import utcnow
from .db import Database


class BriefStore:
    def __init__(self, db: Database):
        self.db = db

    def save(self, title: str, body: str, *, has_ai: bool = False, meta: dict | None = None,
             created_at=None, keep: int = 200) -> str:
        bid = uuid.uuid4().hex
        self.db.conn.execute(
            "INSERT INTO briefs (id, created_at, title, body, has_ai, meta) VALUES (?,?,?,?,?,?)",
            (bid, (created_at or utcnow()).isoformat(), title, body, 1 if has_ai else 0,
             json.dumps(meta or {}, default=str)),
        )
        self.db.conn.commit()
        if keep:
            self.prune(keep)
        return bid

    def recent(self, limit: int = 30) -> list[dict[str, Any]]:
        """Newest first, WITHOUT bodies (list view)."""
        rows = self.db.conn.execute(
            "SELECT id, created_at, title, has_ai, meta, length(body) AS chars FROM briefs "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def get(self, brief_id: str) -> dict[str, Any] | None:
        r = self.db.conn.execute("SELECT * FROM briefs WHERE id = ?", (brief_id,)).fetchone()
        return self._row(r) if r else None

    def latest(self) -> dict[str, Any] | None:
        r = self.db.conn.execute("SELECT * FROM briefs ORDER BY created_at DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def delete(self, brief_id: str) -> bool:
        cur = self.db.conn.execute("DELETE FROM briefs WHERE id = ?", (brief_id,))
        self.db.conn.commit()
        return cur.rowcount == 1

    def prune(self, keep: int) -> int:
        cur = self.db.conn.execute(
            "DELETE FROM briefs WHERE id NOT IN (SELECT id FROM briefs ORDER BY created_at DESC LIMIT ?)",
            (int(keep),))
        self.db.conn.commit()
        return cur.rowcount

    @staticmethod
    def _row(r) -> dict[str, Any]:
        d = dict(r)
        d["has_ai"] = bool(d.get("has_ai"))
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (ValueError, TypeError):
            d["meta"] = {}
        return d
