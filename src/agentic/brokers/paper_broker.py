"""In-memory paper broker for dev, tests, and dry-run mode.

- Seeds a couple of fake short option positions so the read-only monitor works
  end-to-end with no credentials.
- Simulates fills at the order's limit price (immediate full fill) for Phase 2 testing.
- Supports a simple "decay" knob so tests can drive a position toward a profit target.

The fake positions can be overridden by passing ``seed_positions`` (e.g. from a test).
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

from ..domain.enums import (
    Direction,
    OptionType,
    OrderStatus,
    PositionStatus,
    Strategy,
)
from typing import Any

from ..domain.enums import OptionType as _OptionType
from ..domain.models import EquityHolding, Order, Position, utcnow
from ..marketdata.alpaca_md import parse_occ_symbol
from .base import BrokerCapabilities, ExecutionBroker

log = logging.getLogger("agentic.brokers.paper")


def _pos_to_dict(p: Position) -> dict:
    return {
        "occ_symbol": p.occ_symbol, "underlying": p.underlying,
        "option_type": p.option_type.value, "strategy": p.strategy.value,
        "direction": p.direction.value, "quantity": p.quantity, "strike": p.strike,
        "expiration": p.expiration.isoformat(), "credit_received": p.credit_received,
        "open_avg_price": p.open_avg_price, "current_bid": p.current_bid,
        "current_ask": p.current_ask, "current_mark": p.current_mark, "delta": p.delta,
        "iv": p.iv, "peak_profit_pct": p.peak_profit_pct,
        "trough_profit_pct": p.trough_profit_pct, "status": p.status.value,
        "broker_position_id": p.broker_position_id, "option_id": p.option_id,
        "opened_at": p.opened_at.isoformat() if p.opened_at else None,
    }


def _pos_from_dict(d: dict) -> Position:
    return Position(
        occ_symbol=d["occ_symbol"], underlying=d["underlying"],
        option_type=OptionType(d["option_type"]), strategy=Strategy(d["strategy"]),
        direction=Direction(d["direction"]), quantity=d["quantity"], strike=d["strike"],
        expiration=date.fromisoformat(d["expiration"]), credit_received=d["credit_received"],
        open_avg_price=d.get("open_avg_price"), current_bid=d.get("current_bid"),
        current_ask=d.get("current_ask"), current_mark=d.get("current_mark"),
        delta=d.get("delta"), iv=d.get("iv"), peak_profit_pct=d.get("peak_profit_pct", 0.0),
        trough_profit_pct=d.get("trough_profit_pct", 0.0),
        status=PositionStatus(d["status"]), broker_position_id=d.get("broker_position_id"),
        option_id=d.get("option_id"),
        opened_at=datetime.fromisoformat(d["opened_at"]) if d.get("opened_at") else None,
    )


def _default_seed() -> list[Position]:
    today = utcnow().date()
    return [
        Position(
            occ_symbol="AAPL" + (today + timedelta(days=30)).strftime("%y%m%d") + "C00250000",
            underlying="AAPL",
            option_type=OptionType.CALL,
            strategy=Strategy.COVERED_CALL,
            direction=Direction.SHORT,
            quantity=1,
            strike=250.0,
            expiration=today + timedelta(days=30),
            credit_received=2.00,
            open_avg_price=2.00,
            current_bid=1.45,
            current_ask=1.55,
            current_mark=1.50,
            delta=0.30,
            iv=0.28,
            status=PositionStatus.OPEN,
            broker_position_id="paper-aapl-cc",
            opened_at=utcnow(),
        ),
        Position(
            occ_symbol="MSFT" + (today + timedelta(days=15)).strftime("%y%m%d") + "P00400000",
            underlying="MSFT",
            option_type=OptionType.PUT,
            strategy=Strategy.CASH_SECURED_PUT,
            direction=Direction.SHORT,
            quantity=2,
            strike=400.0,
            expiration=today + timedelta(days=15),
            credit_received=3.50,
            open_avg_price=3.50,
            current_bid=2.40,
            current_ask=2.50,
            current_mark=2.45,
            delta=-0.18,
            iv=0.31,
            status=PositionStatus.OPEN,
            broker_position_id="paper-msft-csp",
            opened_at=utcnow(),
        ),
    ]


class PaperBroker(ExecutionBroker):
    def __init__(
        self, seed_positions: list[Position] | None = None, buying_power: float = 100_000.0,
        holdings: list[EquityHolding] | None = None, persist_path: str | Path | None = None,
    ):
        self._persist_path = Path(persist_path) if persist_path else None
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._buying_power = buying_power
        self._holdings: list[EquityHolding] = list(holdings or [])
        # Restore prior paper state so a restart/redeploy doesn't wipe open positions (which
        # reconcile would otherwise book as spurious closes). Falls back to seeds on first run.
        if self._persist_path is not None and self._persist_path.exists():
            self._load()
        else:
            for p in (seed_positions if seed_positions is not None else _default_seed()):
                self._positions[p.occ_symbol] = p

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "positions": [_pos_to_dict(p) for p in self._positions.values()
                              if p.status in (PositionStatus.OPEN, PositionStatus.CLOSING)],
                "holdings": [{"symbol": h.symbol, "quantity": h.quantity,
                              "average_cost": h.average_cost} for h in self._holdings],
            }
            self._persist_path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:  # noqa: BLE001 — persistence is best-effort, never break trading
            log.warning("Paper broker persist failed", exc_info=True)

    def _load(self) -> None:
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            log.warning("Paper broker state load failed; starting empty", exc_info=True)
            return
        for d in data.get("positions", []):
            p = _pos_from_dict(d)
            self._positions[p.occ_symbol] = p
        self._holdings = [
            EquityHolding(symbol=h["symbol"], quantity=h["quantity"],
                          average_cost=h["average_cost"])
            for h in data.get("holdings", [])
        ]
        log.info("Paper broker restored %d position(s) from %s",
                 len(self._positions), self._persist_path)

    async def get_equity_positions(self) -> list[EquityHolding]:
        return list(self._holdings)

    async def connect(self) -> None:
        return None

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="paper",
            supports_options_orders=True,
            is_paper=True,
            notes="Simulated broker; no real orders.",
        )

    async def get_open_positions(self) -> list[Position]:
        return [
            p for p in self._positions.values()
            if p.status in (PositionStatus.OPEN, PositionStatus.CLOSING)
        ]

    async def submit_close_order(self, order: Order) -> Order:
        # Idempotent: same client_order_id returns the existing order.
        if order.client_order_id in self._orders:
            return self._orders[order.client_order_id]
        order.broker_order_id = "paper-" + order.client_order_id[:8]
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.submitted_at = utcnow()
        order.last_status_at = utcnow()
        self._orders[order.client_order_id] = order
        # Reflect the close on the simulated position.
        pos = self._positions.get(order.occ_symbol)
        if pos:
            pos.status = PositionStatus.CLOSED
        self._save()
        return order

    async def get_buying_power(self) -> float:
        return self._buying_power

    async def get_account_value(self) -> float:
        return self._buying_power

    async def review_open_order(self, order: Order) -> dict[str, Any]:
        return {"simulated": True, "occ": order.occ_symbol, "qty": order.quantity,
                "limit": order.limit_price, "side": "sell", "position_effect": "open"}

    async def submit_open_order(self, order: Order) -> Order:
        """Simulate a sell-to-open: fill at limit and materialize a new short-put position."""
        if order.client_order_id in self._orders:
            return self._orders[order.client_order_id]
        order.broker_order_id = "paper-open-" + order.client_order_id[:8]
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.avg_fill_price = order.limit_price
        order.submitted_at = utcnow()
        order.last_status_at = utcnow()
        self._orders[order.client_order_id] = order
        # Materialize the new short CSP so get_open_positions() returns it (reconcile discovers).
        parsed = parse_occ_symbol(order.occ_symbol)
        if parsed is not None:
            root, exp, opt_type, strike = parsed
            self._positions[order.occ_symbol] = Position(
                occ_symbol=order.occ_symbol,
                underlying=root,
                option_type=_OptionType.PUT if opt_type == "put" else _OptionType.CALL,
                strategy=Strategy.CASH_SECURED_PUT if opt_type == "put" else Strategy.COVERED_CALL,
                direction=Direction.SHORT,
                quantity=order.quantity,
                strike=strike,
                expiration=exp,
                credit_received=order.limit_price,
                open_avg_price=order.limit_price,
                current_mark=order.limit_price,
                status=PositionStatus.OPEN,
                option_id=order.option_id,
                broker_position_id="paper-" + order.occ_symbol,
                opened_at=utcnow(),
            )
        self._save()
        return order

    async def get_order(self, order: Order) -> Order:
        return self._orders.get(order.client_order_id, order)

    async def cancel_order(self, order: Order) -> None:
        existing = self._orders.get(order.client_order_id)
        if existing and existing.status in (OrderStatus.PENDING, OrderStatus.SUBMITTED):
            existing.status = OrderStatus.CANCELLED

    # --- test/dev helpers ---
    def set_mark(self, occ_symbol: str, bid: float, ask: float) -> None:
        """Move a position's quoted price (e.g. to trip a profit target in tests)."""
        pos = self._positions.get(occ_symbol)
        if pos:
            pos.current_bid = bid
            pos.current_ask = ask
            pos.current_mark = round((bid + ask) / 2, 4)
