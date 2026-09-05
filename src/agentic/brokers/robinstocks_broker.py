"""robin_stocks fallback broker (unofficial Robinhood API).

WARNING: robin_stocks automates Robinhood without official sanction. It violates
Robinhood's ToS and risks account action. Use only as a deliberate fallback when the
official MCP cannot place option orders, and prefer paper mode while developing.

Reads positions via get_open_option_positions + get_option_market_data; closes via
order_buy_option_limit(positionEffect='close', creditOrDebit='debit', ...). Auth uses
username/password + TOTP from RH_MFA_SECRET. Implemented defensively; methods that
aren't needed until Phase 2 raise NotImplementedError.
"""
from __future__ import annotations

import logging
from datetime import date

from ..config import get_secret
from ..domain.enums import Direction, OptionType, PositionStatus, Strategy
from ..domain.models import Order, Position
from .base import BrokerCapabilities, ExecutionBroker

log = logging.getLogger("agentic.brokers.robinstocks")


class RobinStocksBroker(ExecutionBroker):
    def __init__(self) -> None:
        self._connected = False
        self._rs = None

    async def connect(self) -> None:
        try:
            import robin_stocks.robinhood as rs
        except ImportError:
            log.warning("robin_stocks not installed; install the 'robinhood' extra.")
            return
        username = get_secret("RH_USERNAME")
        password = get_secret("RH_PASSWORD")
        mfa_secret = get_secret("RH_MFA_SECRET")
        if not username or not password:
            log.warning("RH_USERNAME/RH_PASSWORD not set; robin_stocks unavailable.")
            return
        mfa_code = None
        if mfa_secret:
            try:
                import pyotp
                mfa_code = pyotp.TOTP(mfa_secret).now()
            except ImportError:
                log.warning("pyotp not installed; cannot generate MFA code.")
        try:
            rs.login(username, password, mfa_code=mfa_code)
            self._rs = rs
            self._connected = True
            log.info("robin_stocks logged in.")
        except Exception as exc:  # noqa: BLE001
            log.warning("robin_stocks login failed: %s", exc)

    def capabilities(self) -> BrokerCapabilities:
        return BrokerCapabilities(
            name="robinstocks",
            supports_options_orders=self._connected,
            is_paper=False,
            notes="Unofficial; ToS risk. " + ("Connected." if self._connected else "Not connected."),
        )

    async def get_open_positions(self) -> list[Position]:
        if not self._connected or self._rs is None:
            return []
        positions: list[Position] = []
        try:
            raw = self._rs.options.get_open_option_positions()
        except Exception as exc:  # noqa: BLE001
            log.warning("get_open_option_positions failed: %s", exc)
            return []
        for item in raw or []:
            try:
                positions.append(self._map_position(item))
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping unmappable position %s: %s", item, exc)
        return positions

    def _map_position(self, item: dict) -> Position:
        # robin_stocks returns short_quantity/long_quantity; we only close shorts.
        qty = int(float(item.get("quantity", item.get("short_quantity", 0)) or 0))
        opt_type = OptionType.CALL if item.get("type", "call") == "call" else OptionType.PUT
        strategy = Strategy.COVERED_CALL if opt_type == OptionType.CALL else Strategy.CASH_SECURED_PUT
        avg_price = float(item.get("average_price", 0) or 0)
        return Position(
            occ_symbol=item.get("occ_symbol", item.get("chain_symbol", "")),
            underlying=item.get("chain_symbol", ""),
            option_type=opt_type,
            strategy=strategy,
            direction=Direction.SHORT,
            quantity=abs(qty),
            strike=float(item.get("strike_price", 0) or 0),
            expiration=date.fromisoformat(item["expiration_date"]) if item.get("expiration_date") else date.today(),
            credit_received=abs(avg_price) / 100.0 if avg_price else 0.0,
            open_avg_price=abs(avg_price) / 100.0 if avg_price else None,
            status=PositionStatus.OPEN,
            broker_position_id=item.get("id"),
        )

    async def submit_close_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 2: order_buy_option_limit(positionEffect='close', ...)")

    async def get_order(self, order: Order) -> Order:
        raise NotImplementedError("Phase 2")

    async def cancel_order(self, order: Order) -> None:
        raise NotImplementedError("Phase 2: cancel_option_order")
