"""Tax-reserve ledger -- one row per weekly sweep period.

Each period the loop computes the NET realized P&L since the previous period end (plus any loss
carried forward), and either buys ``pct`` of it in the reserve ETF (``filled``), skips because the
net was <= 0 or too small (``skipped``, carrying the balance forward), logs what it would have done
(``dry_run``), or records a broker failure (``failed``). ``UNIQUE(period_end)`` makes a re-run of the
same period a no-op, so a restart can never double-buy.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from ..domain.models import utcnow
from .db import Database


class TaxReserveStore:
    def __init__(self, db: Database):
        self.db = db

    def record(self, *, period_start: str, period_end: str, net_realized: float, carry_in: float,
               carry_out: float, amount_due: float, status: str, symbol: str,
               dollar_amount: float | None = None, shares: float | None = None,
               fill_price: float | None = None, broker_order_id: str | None = None,
               ref_id: str | None = None, error: str | None = None, meta: dict | None = None,
               created_at=None) -> bool:
        """Insert one period row; False when that period_end already has a row."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO tax_reserve
                 (id, created_at, period_start, period_end, net_realized, carry_in, carry_out,
                  amount_due, symbol, dollar_amount, shares, fill_price, broker_order_id, ref_id,
                  status, error, meta)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, (created_at or utcnow()).isoformat(), period_start, period_end,
             round(net_realized, 2), round(carry_in, 2), round(carry_out, 2), round(amount_due, 2),
             symbol.upper(), dollar_amount, shares, fill_price, broker_order_id, ref_id, status,
             error, json.dumps(meta or {}, default=str)),
        )
        self.db.conn.commit()
        return cur.rowcount == 1

    def has_period(self, period_end: str) -> bool:
        r = self.db.conn.execute("SELECT 1 FROM tax_reserve WHERE period_end = ?", (period_end,)).fetchone()
        return r is not None

    def last(self) -> dict[str, Any] | None:
        r = self.db.conn.execute(
            "SELECT * FROM tax_reserve ORDER BY period_end DESC LIMIT 1").fetchone()
        return self._row(r) if r else None

    def recent(self, limit: int = 26) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT * FROM tax_reserve ORDER BY period_end DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def totals(self) -> dict[str, Any]:
        r = self.db.conn.execute(
            "SELECT COUNT(*) AS periods, "
            "COALESCE(SUM(CASE WHEN status='filled' THEN dollar_amount END), 0) AS swept, "
            "COALESCE(SUM(CASE WHEN status='filled' THEN shares END), 0) AS shares, "
            "COALESCE(SUM(net_realized), 0) AS net_realized, "
            "SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) AS sweeps, "
            "MIN(period_start) AS first_period FROM tax_reserve").fetchone()
        last = self.last()
        return {"periods": r["periods"], "sweeps": r["sweeps"] or 0, "swept_dollars": round(r["swept"], 2),
                "shares": round(r["shares"], 6), "net_realized": round(r["net_realized"], 2),
                "carry": (last["carry_out"] if last else 0.0), "first_period": r["first_period"],
                "last_period_end": (last["period_end"] if last else None)}

    @staticmethod
    def _row(r) -> dict[str, Any]:
        d = dict(r)
        try:
            d["meta"] = json.loads(d.get("meta") or "{}")
        except (ValueError, TypeError):
            d["meta"] = {}
        return d
