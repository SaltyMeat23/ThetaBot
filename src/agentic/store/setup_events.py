"""SetupEvent repository -- every technical setup that FIRED, with its forward outcome.

One row per (symbol, label, completed bar, source). The scanner records each ``fired_now`` label
once per completed bar (the unique index makes repeated scans of the same bar a no-op), and later
fills in the forward returns / max excursions once enough bars exist after the fire. This is the
dataset that answers "which patterns actually have edge on THESE names" -- measured, not assumed.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..domain.models import utcnow
from .db import Database


class SetupEventStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, symbol: str, label: str, bar_date: str, source: str, fire_price: float,
               features: dict | None = None, fired_at=None) -> bool:
        """Insert one fire; returns False when that (symbol, label, bar_date, source) already exists."""
        ts = (fired_at or utcnow()).isoformat()
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO setup_events
                 (id, symbol, label, bar_date, source, fired_at, fire_price, features)
               VALUES (?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, symbol.upper(), label, bar_date, source, ts, float(fire_price),
             json.dumps(features or {}, default=str)),
        )
        self.db.conn.commit()
        return cur.rowcount == 1

    def pending(self, symbol: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        """Fires whose forward outcome hasn't been filled in yet."""
        if symbol:
            rows = self.db.conn.execute(
                "SELECT * FROM setup_events WHERE resolved_at IS NULL AND symbol = ? "
                "ORDER BY fired_at LIMIT ?", (symbol.upper(), limit)).fetchall()
        else:
            rows = self.db.conn.execute(
                "SELECT * FROM setup_events WHERE resolved_at IS NULL ORDER BY fired_at LIMIT ?",
                (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def resolve(self, event_id: str, *, ret_5d=None, ret_10d=None, mae_10d=None, mfe_10d=None,
                resolved_at=None) -> None:
        self.db.conn.execute(
            "UPDATE setup_events SET ret_5d=?, ret_10d=?, mae_10d=?, mfe_10d=?, resolved_at=? WHERE id=?",
            (ret_5d, ret_10d, mae_10d, mfe_10d, (resolved_at or utcnow()).isoformat(), event_id))
        self.db.conn.commit()

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM setup_events ORDER BY fired_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def all_events(self, limit: int = 5000) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM setup_events ORDER BY fired_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def accuracy(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Per (label, source): n resolved, hit rate, average forward returns and MAE."""
        from ..services.setup_tracker import aggregate_accuracy
        return aggregate_accuracy(self.all_events(limit))

    @staticmethod
    def _row(r) -> dict[str, Any]:
        d = dict(r)
        try:
            d["features"] = json.loads(d.get("features") or "{}")
        except (ValueError, TypeError):
            d["features"] = {}
        return d
