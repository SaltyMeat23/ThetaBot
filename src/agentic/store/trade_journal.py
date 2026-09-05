"""Trade-journal repository + IV-history seed store (the learning loop's labeled dataset).

Observe-only: rows are written at fill (status 'open') and backfilled at resolution. Nothing here
feeds back into trading decisions — it's the data substrate for later analytics/scoring.
"""
from __future__ import annotations

import json
from datetime import date, datetime

from ..domain.models import TradeJournalEntry, utcnow
from .db import Database

_OUTCOME_STATUSES = ("win", "loss", "expired", "assigned", "called_away", "closed")


def _row_to_entry(r) -> TradeJournalEntry:
    return TradeJournalEntry(
        id=r["id"],
        entry_decision_id=r["entry_decision_id"],
        occ_symbol=r["occ_symbol"],
        underlying=r["underlying"],
        kind=r["kind"],
        contracts=r["contracts"],
        strike=r["strike"],
        dte=r["dte"],
        delta=r["delta"],
        iv=r["iv"],
        premium=r["premium"],
        spread_pct=r["spread_pct"],
        open_interest=r["open_interest"],
        volume=r["volume"],
        annualized_ror=r["annualized_ror"],
        underlying_price=r["underlying_price"],
        context=json.loads(r["context"]) if r["context"] else {},
        status=r["status"],
        realized_pnl=r["realized_pnl"],
        close_price=r["close_price"],
        days_held=r["days_held"],
        exit_reason=r["exit_reason"],
        entered_at=datetime.fromisoformat(r["entered_at"]),
        closed_at=datetime.fromisoformat(r["closed_at"]) if r["closed_at"] else None,
    )


class TradeJournalStore:
    def __init__(self, db: Database):
        self.db = db

    def insert(self, e: TradeJournalEntry) -> None:
        self.db.conn.execute(
            """INSERT INTO trade_journal
                 (id, entry_decision_id, occ_symbol, underlying, kind, contracts, strike, dte,
                  delta, iv, premium, spread_pct, open_interest, volume, annualized_ror,
                  underlying_price, context, status, realized_pnl, close_price, days_held,
                  exit_reason, entered_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                e.id, e.entry_decision_id, e.occ_symbol, e.underlying, e.kind, e.contracts,
                e.strike, e.dte, e.delta, e.iv, e.premium, e.spread_pct, e.open_interest,
                e.volume, e.annualized_ror, e.underlying_price, json.dumps(e.context),
                e.status, e.realized_pnl, e.close_price, e.days_held, e.exit_reason,
                e.entered_at.isoformat(), e.closed_at.isoformat() if e.closed_at else None,
            ),
        )
        self.db.conn.commit()

    def find_open_by_occ(self, occ_symbol: str) -> TradeJournalEntry | None:
        r = self.db.conn.execute(
            "SELECT * FROM trade_journal WHERE occ_symbol = ? AND status = 'open' "
            "ORDER BY entered_at DESC LIMIT 1",
            (occ_symbol,),
        ).fetchone()
        return _row_to_entry(r) if r else None

    def set_outcome(
        self,
        journal_id: str,
        *,
        status: str,
        realized_pnl: float | None = None,
        close_price: float | None = None,
        exit_reason: str | None = None,
        entered_at: datetime | None = None,
        mfe_pct: float | None = None,
        mae_pct: float | None = None,
    ) -> None:
        now = utcnow()
        days_held = (now - entered_at).days if entered_at is not None else None
        # MFE/MAE (max favorable/adverse excursion over the hold) go into the open ``context`` JSON —
        # no schema change — so they ride the refinement export and analytics like any other feature.
        # They reveal exit timing: a trade whose MFE far exceeds its realized P&L was exited late.
        if mfe_pct is not None or mae_pct is not None:
            row = self.db.conn.execute(
                "SELECT context FROM trade_journal WHERE id = ?", (journal_id,)).fetchone()
            ctx = {}
            if row and row["context"]:
                try:
                    ctx = json.loads(row["context"]) or {}
                except (ValueError, TypeError):
                    ctx = {}
            if mfe_pct is not None:
                ctx["mfe_pct"] = round(mfe_pct, 4)
            if mae_pct is not None:
                ctx["mae_pct"] = round(mae_pct, 4)
            self.db.conn.execute(
                "UPDATE trade_journal SET context = ? WHERE id = ?",
                (json.dumps(ctx), journal_id))
        self.db.conn.execute(
            """UPDATE trade_journal SET
                 status = ?, realized_pnl = ?, close_price = ?, exit_reason = ?,
                 days_held = ?, closed_at = ?
               WHERE id = ?""",
            (status, realized_pnl, close_price, exit_reason, days_held, now.isoformat(), journal_id),
        )
        self.db.conn.commit()

    def realized_since(self, since_iso: str) -> tuple[float, int]:
        """(sum, count) of realized P&L for trades RESOLVED on/after ``since_iso`` (by close time).
        Feeds the loss circuit breaker's rolling-window realized-loss check."""
        rows = self.db.conn.execute(
            "SELECT realized_pnl FROM trade_journal "
            "WHERE closed_at IS NOT NULL AND closed_at >= ? AND realized_pnl IS NOT NULL",
            (since_iso,),
        ).fetchall()
        return round(sum(r["realized_pnl"] for r in rows), 2), len(rows)

    def resolved_pnls(self, limit: int = 50) -> list[float]:
        """Realized P&L of the most-recently-CLOSED trades, newest first — for a loss-streak read."""
        rows = self.db.conn.execute(
            "SELECT realized_pnl FROM trade_journal "
            "WHERE closed_at IS NOT NULL AND realized_pnl IS NOT NULL "
            "ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [r["realized_pnl"] for r in rows]

    def recent(self, limit: int = 200) -> list[TradeJournalEntry]:
        rows = self.db.conn.execute(
            "SELECT * FROM trade_journal ORDER BY entered_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_row_to_entry(r) for r in rows]

    def purge_incomplete(self) -> int:
        """One-time cleanup: drop journal rows for positions wiped by a redeploy (status 'closed'
        with no realized P&L) — not real trade outcomes. Returns the number removed."""
        cur = self.db.conn.execute(
            "DELETE FROM trade_journal WHERE status = 'closed' AND realized_pnl IS NULL"
        )
        self.db.conn.commit()
        return cur.rowcount

    # --- IV history (seed for IV Rank, computed in a later phase) ---
    def record_iv(self, symbol: str, day: date, atm_iv: float) -> None:
        """Upsert one day's ATM IV for a symbol (idempotent across intraday scans)."""
        self.db.conn.execute(
            "INSERT INTO iv_history (symbol, date, atm_iv) VALUES (?,?,?) "
            "ON CONFLICT(symbol, date) DO UPDATE SET atm_iv = excluded.atm_iv",
            (symbol.upper(), day.isoformat(), atm_iv),
        )
        self.db.conn.commit()

    def iv_history(self, symbol: str, limit: int = 504) -> list[tuple[str, float]]:
        rows = self.db.conn.execute(
            "SELECT date, atm_iv FROM iv_history WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (symbol.upper(), limit),
        ).fetchall()
        return [(r["date"], r["atm_iv"]) for r in rows]
