"""Append-only audit log repository."""
from __future__ import annotations

import json
import logging
from typing import Any

from ..domain.enums import AuditEventType
from ..domain.models import AuditEvent, utcnow
from .db import Database

log = logging.getLogger("agentic.audit")


class AuditStore:
    def __init__(self, db: Database):
        self.db = db

    def record(
        self,
        event_type: AuditEventType,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "system",
        position_id: str | None = None,
        decision_id: str | None = None,
        order_id: str | None = None,
    ) -> None:
        event = AuditEvent(
            event_type=event_type,
            payload=payload or {},
            source=source,
            position_id=position_id,
            decision_id=decision_id,
            order_id=order_id,
        )
        self.db.conn.execute(
            """INSERT INTO audit (ts, event_type, source, position_id, decision_id, order_id, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                event.ts.isoformat(),
                event.event_type.value,
                event.source,
                event.position_id,
                event.decision_id,
                event.order_id,
                json.dumps(event.payload, default=str),
            ),
        )
        self.db.conn.commit()
        log.debug("audit %s %s", event.event_type.value, event.payload)

    def latest(
        self, event_type: AuditEventType | None = None, source: str | None = None
    ) -> dict[str, Any] | None:
        """The most recent audit event matching an optional event_type and/or source.

        Used by the ops endpoint to read each loop's last-run timestamp (and the last reconcile's
        sync payload) without a broker round-trip.
        """
        clauses, params = [], []
        if event_type is not None:
            clauses.append("event_type = ?")
            params.append(event_type.value)
        if source is not None:
            clauses.append("source = ?")
            params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        row = self.db.conn.execute(
            f"SELECT * FROM audit{where} ORDER BY id DESC LIMIT 1", params
        ).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        return d

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out
