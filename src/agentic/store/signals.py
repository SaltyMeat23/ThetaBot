"""Signal repository — inbound TradingView webhook alerts (Phase 3).

The webhook handler does almost no work: it validates the token, builds a ``dedup_key``
(the alert id, or a sha256 of the body), and inserts a Signal row. The monitor consumes
NEW signals on its next cycle. The UNIQUE index on ``dedup_key`` makes duplicate webhook
deliveries (TradingView retries) no-ops.
"""
from __future__ import annotations

import json
from datetime import datetime

from ..domain.enums import SignalStatus
from ..domain.models import Signal
from .db import Database


def _row_to_signal(r) -> Signal:
    return Signal(
        id=r["id"],
        raw=json.loads(r["raw"]),
        dedup_key=r["dedup_key"],
        token_ok=bool(r["token_ok"]),
        action=r["action"],
        match_field=r["match_field"],
        match_value=r["match_value"],
        status=SignalStatus(r["status"]),
        received_at=datetime.fromisoformat(r["received_at"]),
        ttl_expires_at=datetime.fromisoformat(r["ttl_expires_at"]) if r["ttl_expires_at"] else None,
    )


class SignalStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, s: Signal) -> bool:
        """Insert the signal. Returns True if newly inserted, False on duplicate dedup_key."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO signals
                 (id, raw, dedup_key, token_ok, action, match_field, match_value,
                  status, received_at, ttl_expires_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                s.id, json.dumps(s.raw, default=str), s.dedup_key, 1 if s.token_ok else 0,
                s.action, s.match_field, s.match_value, s.status.value,
                s.received_at.isoformat(),
                s.ttl_expires_at.isoformat() if s.ttl_expires_at else None,
            ),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def list_by_status(self, status: SignalStatus) -> list[Signal]:
        rows = self.db.conn.execute(
            "SELECT * FROM signals WHERE status = ? ORDER BY received_at", (status.value,)
        ).fetchall()
        return [_row_to_signal(r) for r in rows]

    def set_status(self, signal_id: str, status: SignalStatus) -> None:
        self.db.conn.execute(
            "UPDATE signals SET status = ? WHERE id = ?", (status.value, signal_id)
        )
        self.db.conn.commit()

    def get(self, signal_id: str) -> Signal | None:
        r = self.db.conn.execute(
            "SELECT * FROM signals WHERE id = ?", (signal_id,)
        ).fetchone()
        return _row_to_signal(r) if r else None
