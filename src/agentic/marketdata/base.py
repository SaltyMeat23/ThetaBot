"""MarketDataProvider interface + a paper provider for dev/tests.

The paper provider derives a quote from the position's own broker-reported marks so the
read-only monitor works end-to-end with no external data subscription. Alpaca is the
real provider (see alpaca_md.py)."""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..domain.models import Position
from .quote import OptionContractQuote, OptionQuote


class MarketDataProvider(ABC):
    @abstractmethod
    async def get_quote(self, position: Position) -> OptionQuote | None:
        """Return a fresh OptionQuote for the position's contract, or None if unavailable."""
        raise NotImplementedError

    # --- entry-side (chain scanning); default no-op so close-only providers still work ---
    async def get_chain(self, underlying: str) -> list[OptionContractQuote]:
        """Return all tradable contracts for an underlying (entry screening). Default: none."""
        return []

    async def get_underlying_price(self, underlying: str) -> float | None:
        """Return the underlying's last/mark price (for moneyness). Default: unavailable."""
        return None

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260) -> list[dict]:
        """Return daily bars [{o,h,l,c,v}...] oldest->newest for technicals. Default: none."""
        return []


class PaperMarketData(MarketDataProvider):
    """Fabricates a quote from the broker-reported mark/bid/ask on the position.

    Useful in paper mode and tests: whatever the (paper) broker says the position is
    worth becomes the quote. No network calls.
    """

    async def get_quote(self, position: Position) -> OptionQuote | None:
        mark = position.current_mark
        bid = position.current_bid
        ask = position.current_ask
        if mark is None and bid is not None and ask is not None:
            mark = round((bid + ask) / 2, 4)
        if bid is None and mark is not None:
            bid = round(mark * 0.97, 4)
        if ask is None and mark is not None:
            ask = round(mark * 1.03, 4)
        if mark is None and bid is None and ask is None:
            return None
        return OptionQuote(
            occ_symbol=position.occ_symbol,
            bid=bid,
            ask=ask,
            mark=mark,
            delta=position.delta,
            iv=position.iv,
        )
