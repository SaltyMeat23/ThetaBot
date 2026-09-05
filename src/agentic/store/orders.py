"""Order repository.

The executor persists an Order row in PENDING state **before** it submits to the broker,
keyed by ``client_order_id`` (UNIQUE). This is the idempotency backstop: if we crash
between persist and submit, the row tells us a submit may be in flight, and reusing the
same ``client_order_id`` on retry prevents a duplicate broker order.
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import OrderStatus
from ..domain.models import Order
from .db import Database


def _row_to_order(r) -> Order:
    return Order(
        id=r["id"],
        decision_id=r["decision_id"],
        position_id=r["position_id"],
        client_order_id=r["client_order_id"],
        broker_order_id=r["broker_order_id"],
        option_id=r["option_id"],
        occ_symbol=r["occ_symbol"],
        side=r["side"],
        order_type=r["order_type"],
        quantity=r["quantity"],
        limit_price=r["limit_price"],
        filled_qty=r["filled_qty"],
        avg_fill_price=r["avg_fill_price"],
        status=OrderStatus(r["status"]),
        is_paper=bool(r["is_paper"]),
        submitted_at=datetime.fromisoformat(r["submitted_at"]) if r["submitted_at"] else None,
        last_status_at=datetime.fromisoformat(r["last_status_at"]) if r["last_status_at"] else None,
    )


class OrderStore:
    def __init__(self, db: Database):
        self.db = db

    def insert_if_new(self, o: Order) -> bool:
        """Persist a PENDING order. Returns True if newly inserted, False if a row with the
        same client_order_id already exists (idempotent submit guard)."""
        cur = self.db.conn.execute(
            """INSERT OR IGNORE INTO orders
                 (id, decision_id, position_id, client_order_id, broker_order_id, option_id,
                  occ_symbol, side, order_type, quantity, limit_price, filled_qty, avg_fill_price,
                  status, is_paper, submitted_at, last_status_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                o.id, o.decision_id, o.position_id, o.client_order_id, o.broker_order_id,
                o.option_id, o.occ_symbol, o.side, o.order_type, o.quantity, o.limit_price,
                o.filled_qty, o.avg_fill_price, o.status.value, 1 if o.is_paper else 0,
                o.submitted_at.isoformat() if o.submitted_at else None,
                o.last_status_at.isoformat() if o.last_status_at else None,
            ),
        )
        self.db.conn.commit()
        return cur.rowcount > 0

    def update(self, o: Order) -> None:
        """Persist the latest broker-reported state of an order."""
        self.db.conn.execute(
            """UPDATE orders SET
                 broker_order_id = ?, status = ?, filled_qty = ?, avg_fill_price = ?,
                 limit_price = ?, submitted_at = ?, last_status_at = ?
               WHERE client_order_id = ?""",
            (
                o.broker_order_id, o.status.value, o.filled_qty, o.avg_fill_price,
                o.limit_price,
                o.submitted_at.isoformat() if o.submitted_at else None,
                o.last_status_at.isoformat() if o.last_status_at else None,
                o.client_order_id,
            ),
        )
        self.db.conn.commit()

    def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        r = self.db.conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?", (client_order_id,)
        ).fetchone()
        return _row_to_order(r) if r else None

    def list_all(self) -> list[Order]:
        """All orders, newest first — for the dashboard / P&L attribution."""
        rows = self.db.conn.execute(
            "SELECT * FROM orders ORDER BY COALESCE(submitted_at, '') DESC"
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def list_by_position(self, position_id: str) -> list[Order]:
        """All orders for a position (for close-P&L attribution during reconcile settlement)."""
        rows = self.db.conn.execute(
            "SELECT * FROM orders WHERE position_id = ? ORDER BY submitted_at", (position_id,)
        ).fetchall()
        return [_row_to_order(r) for r in rows]

    def list_by_status(self, *statuses: OrderStatus) -> list[Order]:
        if not statuses:
            return []
        placeholders = ",".join("?" for _ in statuses)
        rows = self.db.conn.execute(
            f"SELECT * FROM orders WHERE status IN ({placeholders}) ORDER BY submitted_at",
            tuple(s.value for s in statuses),
        ).fetchall()
        return [_row_to_order(r) for r in rows]
