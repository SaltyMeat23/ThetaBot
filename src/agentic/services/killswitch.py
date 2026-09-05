"""Global kill switch / pause flag, backed by the SQLite control row.

When paused, the executor refuses to submit orders and the monitor skips decision
execution. Read-only polling and reconciliation continue. State is persisted so a
restart preserves a paused state (fail-safe)."""
from __future__ import annotations

import logging

from ..domain.enums import AuditEventType
from ..domain.models import utcnow
from ..store.audit import AuditStore
from ..store.db import Database

log = logging.getLogger("agentic.killswitch")


class KillSwitch:
    def __init__(self, db: Database, audit: AuditStore, auto_trip_threshold: int = 0):
        self.db = db
        self.audit = audit
        # Optional safety: auto-engage after N consecutive broker errors (0 = disabled).
        self.auto_trip_threshold = auto_trip_threshold
        self._consecutive_errors = 0

    def record_broker_error(self, where: str = "") -> None:
        """Count a broker/cycle failure; auto-engage the switch if the streak hits the
        threshold. A later clean cycle resets the streak via ``record_success``."""
        if self.auto_trip_threshold <= 0:
            return
        self._consecutive_errors += 1
        if self._consecutive_errors >= self.auto_trip_threshold and not self.is_paused():
            self.pause(
                f"auto-trip: {self._consecutive_errors} consecutive broker errors"
                + (f" ({where})" if where else "")
            )

    def record_success(self) -> None:
        self._consecutive_errors = 0

    def consecutive_errors(self) -> int:
        """Current streak of consecutive broker/cycle errors (0 after any clean cycle)."""
        return self._consecutive_errors

    def is_paused(self) -> bool:
        row = self.db.conn.execute("SELECT paused FROM control WHERE id = 1").fetchone()
        return bool(row and row["paused"])

    def reason(self) -> str | None:
        row = self.db.conn.execute("SELECT reason FROM control WHERE id = 1").fetchone()
        return row["reason"] if row else None

    def pause(self, reason: str = "manual") -> None:
        self._set(True, reason)
        log.warning("KILL SWITCH ENGAGED: %s", reason)

    def resume(self, reason: str = "manual") -> None:
        self._set(False, reason)
        log.info("Kill switch released: %s", reason)

    def _set(self, paused: bool, reason: str) -> None:
        self.db.conn.execute(
            "UPDATE control SET paused = ?, reason = ?, updated_at = ? WHERE id = 1",
            (1 if paused else 0, reason, utcnow().isoformat()),
        )
        self.db.conn.commit()
        self.audit.record(
            AuditEventType.KILLSWITCH,
            {"paused": paused, "reason": reason},
            source="killswitch",
        )
