"""Alpaca options execution broker (stub).

Alpaca offers an OFFICIAL, sanctioned options trading API — no ToS risk — but it trades
in an Alpaca account, not Robinhood. Kept as a stub so it can become the primary
execution venue if you decide to move/mirror positions to Alpaca. Fleshed out in Phase 4.
"""
from __future__ import annotations

import logging

from ..domain.models import Order, Position
from .base import BrokerCapabilities, ExecutionBroker

log = logging.getLogger("agentic.brokers.alpaca")


class AlpacaOptionsBroker(ExecutionBroker):
    async def connect(self) -> None:
        log.info("AlpacaOptionsBroker is a stub (Phase 4).")

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="alpaca",
            supports_options_orders=False,
            is_paper=False,
            notes="Stub — official options API, Alpaca account only. Implement in Phase 4.",
        )

    async def get_open_positions(self) -> list[Position]:
        return []

    async def submit_close_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 4")

    async def get_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 4")

    async def cancel_order(self, order: Order) -> None:
        raise NotImplementedError("Phase 4")
