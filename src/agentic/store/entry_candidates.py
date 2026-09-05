"""EntryCandidate repository — the scan-time disposition log.

One row per (scan cycle, screened contract): every candidate that passed the screener, whether
it was approved+sized or rejected, and the exact reason. This is the negative-example dataset
for refining entry logic — the "showed up but never entered" cases the trade journal never sees.
"""
from __future__ import annotations

import uuid
from typing import Any

from ..domain.models import utcnow
from .db import Database


class EntryCandidateStore:
    def __init__(self, db: Database):
        self.db = db

    def record_scan(self, scan_id: str, rows: list[dict[str, Any]], scanned_at=None) -> int:
        """Persist all candidate rows from one scan. Each row dict carries the candidate fields
        plus ``approved`` (bool), ``reason`` (str), and optional ``contracts``. Returns the count.
        """
        ts = (scanned_at or utcnow())
        ts_iso = ts.isoformat()
        day = ts.date().isoformat()
        n = 0
        for r in rows:
            self.db.conn.execute(
                """INSERT INTO entry_candidates
                     (id, scan_id, scanned_at, underlying, occ_symbol, kind, strike, expiration,
                      dte, delta, iv, premium, annualized_ror, open_interest, volume, score,
                      approved, contracts, reason, scanned_date)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex, scan_id, ts_iso, r["underlying"], r["occ_symbol"],
                    r["kind"], r["strike"], r["expiration"], r["dte"], r.get("delta"),
                    r.get("iv"), r.get("premium"), r.get("annualized_ror"),
                    r.get("open_interest"), r.get("volume"), r.get("score"),
                    1 if r.get("approved") else 0, r.get("contracts"), r["reason"], day,
                ),
            )
            n += 1
        self.db.conn.commit()
        return n

    def recent(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM entry_candidates ORDER BY scanned_at DESC, id LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["approved"] = bool(d["approved"])
            out.append(d)
        return out
