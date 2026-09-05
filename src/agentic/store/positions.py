"""Position repository — upsert by OCC symbol, list, mark closed."""
from __future__ import annotations

from datetime import date, datetime

from ..domain.enums import Direction, OptionType, PositionStatus, Strategy
from ..domain.models import Position, utcnow
from .db import Database


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _row_to_position(r) -> Position:
    return Position(
        id=r["id"],
        broker_position_id=r["broker_position_id"],
        option_id=r["option_id"],
        occ_symbol=r["occ_symbol"],
        underlying=r["underlying"],
        option_type=OptionType(r["option_type"]),
        strategy=Strategy(r["strategy"]),
        direction=Direction(r["direction"]),
        quantity=r["quantity"],
        strike=r["strike"],
        expiration=date.fromisoformat(r["expiration"]),
        credit_received=r["credit_received"],
        open_avg_price=r["open_avg_price"],
        current_bid=r["current_bid"],
        current_ask=r["current_ask"],
        current_mark=r["current_mark"],
        delta=r["delta"],
        iv=r["iv"],
        peak_profit_pct=r["peak_profit_pct"] if "peak_profit_pct" in r.keys() else 0.0,
        trough_profit_pct=r["trough_profit_pct"] if "trough_profit_pct" in r.keys() else 0.0,
        is_paper=(None if ("is_paper" not in r.keys() or r["is_paper"] is None)
                  else bool(r["is_paper"])),
        status=PositionStatus(r["status"]),
        opened_at=datetime.fromisoformat(r["opened_at"]) if r["opened_at"] else None,
        last_synced_at=datetime.fromisoformat(r["last_synced_at"]) if r["last_synced_at"] else None,
    )


class PositionStore:
    def __init__(self, db: Database):
        self.db = db

    def upsert(self, p: Position) -> None:
        """Update the currently-OPEN episode of this contract, or insert a new one.

        Keyed by (occ_symbol, still-open) rather than occ_symbol alone: re-selling a contract after
        a close inserts a NEW row so the closed trade's record (and P&L) survives. Preserves the
        existing id on update so decisions/orders referencing the position stay valid.
        """
        p.last_synced_at = utcnow()
        existing = self.db.conn.execute(
            "SELECT id FROM positions WHERE occ_symbol = ? AND status IN (?, ?) "
            "ORDER BY COALESCE(opened_at, last_synced_at) DESC LIMIT 1",
            (p.occ_symbol, PositionStatus.OPEN.value, PositionStatus.CLOSING.value),
        ).fetchone()
        if existing:
            p.id = existing["id"]
        ip = None if p.is_paper is None else (1 if p.is_paper else 0)
        self.db.conn.execute(
            """INSERT INTO positions
                 (id, broker_position_id, option_id, occ_symbol, underlying, option_type, strategy,
                  direction, quantity, strike, expiration, credit_received, open_avg_price,
                  current_bid, current_ask, current_mark, delta, iv, peak_profit_pct,
                  trough_profit_pct, is_paper, status, opened_at, last_synced_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                  broker_position_id=excluded.broker_position_id,
                  option_id=excluded.option_id,
                  quantity=excluded.quantity,
                  -- is_paper self-heals to the active broker on each sync (COALESCE keeps a known
                  -- value if a later read can't determine it).
                  is_paper=COALESCE(excluded.is_paper, positions.is_paper),
                  -- Let the broker's (now correctly-scaled) credit flow through on re-sync so a
                  -- reconcile-discovered position self-heals; _map_position returns None on a
                  -- missing credit, so a bad read can never blank it.
                  credit_received=excluded.credit_received,
                  open_avg_price=excluded.open_avg_price,
                  current_bid=excluded.current_bid,
                  current_ask=excluded.current_ask,
                  current_mark=excluded.current_mark,
                  delta=excluded.delta,
                  iv=excluded.iv,
                  peak_profit_pct=excluded.peak_profit_pct,
                  trough_profit_pct=excluded.trough_profit_pct,
                  status=excluded.status,
                  last_synced_at=excluded.last_synced_at""",
            (
                p.id, p.broker_position_id, p.option_id, p.occ_symbol, p.underlying,
                p.option_type.value, p.strategy.value, p.direction.value, p.quantity,
                p.strike, p.expiration.isoformat(), p.credit_received, p.open_avg_price,
                p.current_bid, p.current_ask, p.current_mark, p.delta, p.iv, p.peak_profit_pct,
                p.trough_profit_pct, ip, p.status.value, _iso(p.opened_at), _iso(p.last_synced_at),
            ),
        )
        self.db.conn.commit()

    def list_open(self) -> list[Position]:
        rows = self.db.conn.execute(
            "SELECT * FROM positions WHERE status IN (?, ?)",
            (PositionStatus.OPEN.value, PositionStatus.CLOSING.value),
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def list_all(self) -> list[Position]:
        """All positions (open and closed), newest activity first — for the dashboard."""
        rows = self.db.conn.execute(
            "SELECT * FROM positions ORDER BY COALESCE(last_synced_at, opened_at) DESC"
        ).fetchall()
        return [_row_to_position(r) for r in rows]

    def get_by_occ(self, occ_symbol: str) -> Position | None:
        """The managed episode for a contract: the open one if any, else the most recent."""
        r = self.db.conn.execute(
            "SELECT * FROM positions WHERE occ_symbol = ? "
            "ORDER BY (status IN ('OPEN','CLOSING')) DESC, "
            "COALESCE(last_synced_at, opened_at) DESC LIMIT 1",
            (occ_symbol,),
        ).fetchone()
        return _row_to_position(r) if r else None

    def get(self, position_id: str) -> Position | None:
        r = self.db.conn.execute(
            "SELECT * FROM positions WHERE id = ?", (position_id,)
        ).fetchone()
        return _row_to_position(r) if r else None

    def set_status(self, position_id: str, status: PositionStatus) -> None:
        self.db.conn.execute(
            "UPDATE positions SET status = ?, last_synced_at = ? WHERE id = ?",
            (status.value, utcnow().isoformat(), position_id),
        )
        self.db.conn.commit()

    def purge_stale(self) -> int:
        """One-time cleanup of P&L noise: remove non-open positions with no real outcome — the
        reconcile-wipe / re-entry-overwrite artifacts (CLOSED with no fill AND no mark) and the
        demo seed fixtures. Never touches OPEN/CLOSING rows, real closed wins (a FILLED order
        exists), EXPIRED/ASSIGNED outcomes, or reconcile-detected closes carrying a last mark —
        stats estimates their realized P&L from ``current_mark`` (see services/stats.py), so they
        are real history, not artifacts. Returns the number of rows removed."""
        cur = self.db.conn.execute(
            """DELETE FROM positions
               WHERE status NOT IN ('OPEN', 'CLOSING')
                 AND (
                   (status = 'CLOSED'
                    AND current_mark IS NULL
                    AND id NOT IN (SELECT position_id FROM orders WHERE status = 'FILLED'))
                   OR broker_position_id IN ('paper-aapl-cc', 'paper-msft-csp')
                 )"""
        )
        self.db.conn.commit()
        return cur.rowcount
