"""Latest TradingView indicator snapshot per symbol — the entry-context channel.

Separate from `Signal` (which is close-only and dedup-queued). A TradingView alert with
``action == "indicator"`` upserts its payload here (one row per symbol). Incoming fields are
MERGED over the stored snapshot (new keys overwrite, untouched keys persist), so several studies
per symbol — e.g. a support/resistance alert plus a separate ADX/features alert — accumulate into
one feature vector instead of clobbering each other. The AI reviewer reads
``get_latest(symbol, max_age_seconds)`` and simply omits it when missing/stale.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..domain.models import utcnow
from .db import Database


class TVIndicatorStore:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, symbol: str, payload: dict[str, Any]) -> None:
        """Merge ``payload`` into the symbol's stored snapshot (new keys overwrite, untouched keys
        persist), so multiple TradingView studies per symbol accumulate into one feature vector
        rather than clobbering each other. ``received_at`` is bumped to now on every write.

        Trade-off: a field that stops being sent lingers (last value persists) — acceptable for a
        best-effort context channel; the AI reviewer already drops the whole snapshot once stale.
        """
        sym = symbol.upper()
        prior = self.db.conn.execute(
            "SELECT raw FROM tv_indicators WHERE symbol = ?", (sym,)
        ).fetchone()
        merged: dict[str, Any] = {}
        if prior is not None:
            try:
                loaded = json.loads(prior["raw"])
                if isinstance(loaded, dict):
                    merged.update(loaded)
            except (ValueError, TypeError):
                pass
        merged.update(payload)
        self.db.conn.execute(
            """INSERT INTO tv_indicators (symbol, raw, received_at) VALUES (?, ?, ?)
               ON CONFLICT(symbol) DO UPDATE SET
                   raw = excluded.raw, received_at = excluded.received_at""",
            (sym, json.dumps(merged), utcnow().isoformat()),
        )
        self.db.conn.commit()

    def recent(self, limit: int = 50) -> list[dict]:
        """All stored indicator snapshots, newest first — for verifying what TradingView sent."""
        rows = self.db.conn.execute(
            "SELECT symbol, raw, received_at FROM tv_indicators ORDER BY received_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            age = (utcnow() - datetime.fromisoformat(r["received_at"])).total_seconds()
            out.append({"symbol": r["symbol"], "received_at": r["received_at"],
                        "age_seconds": round(age, 1), "payload": json.loads(r["raw"])})
        return out

    def get_latest(self, symbol: str, max_age_seconds: int | None = None) -> dict | None:
        """Latest snapshot for a symbol, or None if absent or older than max_age_seconds."""
        r = self.db.conn.execute(
            "SELECT raw, received_at FROM tv_indicators WHERE symbol = ?", (symbol.upper(),)
        ).fetchone()
        if r is None:
            return None
        age = (utcnow() - datetime.fromisoformat(r["received_at"])).total_seconds()
        if max_age_seconds is not None and age > max_age_seconds:
            return None
        return {
            "symbol": symbol.upper(),
            "received_at": r["received_at"],
            "age_seconds": round(age, 1),
            "payload": json.loads(r["raw"]),
        }
