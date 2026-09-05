"""Recent news / catalyst items per symbol — the entry-context news channel.

Advisory only: this feeds the AI reviewer and a logged context feature, and (later) an opt-in
defensive gate. It is NEVER a trade-picker. Ingest is idempotent on ``dedup_key`` so the Alpaca
pull and the webhook push can both re-send the same item without duplicating it.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..domain.models import utcnow
from .db import Database


class NewsStore:
    def __init__(self, db: Database):
        self.db = db

    def add(self, *, symbol: str, headline: str, dedup_key: str,
            source: str | None = None, url: str | None = None,
            created_at: str | None = None) -> bool:
        """Insert one item (idempotent). Returns True if it was new."""
        now = utcnow().isoformat()
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO news_items
                   (dedup_key, symbol, headline, source, url, created_at, received_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (dedup_key, symbol.upper(), headline, source, url, created_at or now, now),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def add_many(self, items: list[dict[str, Any]]) -> int:
        """Insert many items (idempotent); returns the count actually inserted."""
        n = 0
        for it in items:
            if it.get("headline") and it.get("symbol") and it.get("dedup_key"):
                n += 1 if self.add(**it) else 0
        return n

    def recent_for(self, symbol: str, *, max_age_seconds: int | None = None,
                   limit: int = 5) -> list[dict[str, Any]]:
        """Most-recent items for a symbol (newest first), optionally within a freshness window."""
        rows = self.db.conn.execute(
            """SELECT symbol, headline, source, url, created_at, received_at
                   FROM news_items WHERE symbol = ?
                   ORDER BY created_at DESC LIMIT ?""",
            (symbol.upper(), limit),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            if max_age_seconds is not None:
                try:
                    age = (utcnow() - datetime.fromisoformat(r["created_at"])).total_seconds()
                except (ValueError, TypeError):
                    age = 0.0
                if age > max_age_seconds:
                    continue
            out.append({"symbol": r["symbol"], "headline": r["headline"], "source": r["source"],
                        "url": r["url"], "created_at": r["created_at"]})
        return out

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        """All recent items across symbols (newest received first) — for the diagnostic endpoint."""
        rows = self.db.conn.execute(
            """SELECT symbol, headline, source, url, created_at, received_at
                   FROM news_items ORDER BY received_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [{"symbol": r["symbol"], "headline": r["headline"], "source": r["source"],
                 "url": r["url"], "created_at": r["created_at"], "received_at": r["received_at"]}
                for r in rows]
