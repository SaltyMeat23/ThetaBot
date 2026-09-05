"""The broker-agnostic execution interface.

Every venue (Robinhood MCP, robin_stocks, Alpaca, paper) implements ``ExecutionBroker``.
The rest of the app depends only on this interface, so the "which broker / does RH
support options yet" question is fully contained here. ``capabilities()`` reports
whether the broker can place option orders — the factory probes this at startup.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from ..domain.models import EquityHolding, Order, Position


@dataclass
class BrokerCapabilities:
    name: str
    supports_options_orders: bool
    is_paper: bool
    notes: str = ""


class ExecutionBroker(ABC):
    @abstractmethod
    async def connect(self) -> None:
        """Establish session / probe capabilities. Safe to call once at startup."""

    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Report what this broker can do (populated after connect())."""

    @abstractmethod
    async def get_open_positions(self) -> list[Position]:
        """Return current open OPTION positions from the broker (source of truth)."""

    @abstractmethod
    async def submit_close_order(self, order: Order) -> Order:
        """Submit a buy-to-close order. Must be idempotent on order.client_order_id.

        Returns the order updated with broker_order_id and status. Phase 2+.
        """

    @abstractmethod
    async def get_order(self, order: Order) -> Order:
        """Refresh and return the latest state of a previously submitted order."""

    @abstractmethod
    async def cancel_order(self, order: Order) -> None:
        """Cancel a working order (best-effort)."""

    # --- entry-side (opening CSPs); default no-op/unsupported so close-only brokers work ---
    async def get_buying_power(self) -> float:
        """Cash/buying power available for new cash-secured puts. Default: 0 (no entries)."""
        return 0.0

    async def get_account_value(self) -> float:
        """Total account value (for per-name % sizing). Default: falls back to buying power."""
        return await self.get_buying_power()

    async def get_equity_positions(self) -> list[EquityHolding]:
        """Long share positions (for covered calls + assignment detection). Default: none."""
        return []

    async def resolve_option_id(self, occ_symbol: str) -> str | None:
        """Resolve an OCC symbol to this broker's option-instrument id (for opening a new
        contract we don't already hold). Default: unknown."""
        return None

    async def submit_open_order(self, order: Order) -> Order:
        """Submit a sell-to-open order (CSP). Idempotent on order.client_order_id."""
        raise NotImplementedError("This broker does not support opening orders.")

    async def review_open_order(self, order: Order) -> dict[str, Any]:
        """Dry-run a sell-to-open (no order placed). Used to confirm acceptance before live."""
        raise NotImplementedError("This broker does not support opening orders.")
