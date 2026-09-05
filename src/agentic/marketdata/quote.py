"""Option quote model + staleness helper."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from ..domain.models import utcnow


@dataclass
class OptionQuote:
    occ_symbol: str
    bid: float | None
    ask: float | None
    mark: float | None
    delta: float | None = None
    iv: float | None = None
    as_of: datetime = field(default_factory=utcnow)

    @property
    def is_valid(self) -> bool:
        """A usable quote for pricing a close: positive bid and ask present."""
        return self.bid is not None and self.ask is not None and self.bid > 0

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return (now - self.as_of).total_seconds()

    def is_stale(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > max_age_seconds

    @property
    def midpoint(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return self.mark


@dataclass
class OptionContractQuote:
    """One contract from an option-chain scan (entry-side screening input).

    Unlike OptionQuote (a quote for a contract we hold), this carries the full contract
    identity (strike/expiration/type) plus liquidity fields needed to screen CSP candidates.
    """

    occ_symbol: str
    underlying: str
    option_id: str | None
    option_type: str                     # "call" | "put"
    strike: float
    expiration: date
    bid: float | None
    ask: float | None
    mark: float | None
    delta: float | None = None
    iv: float | None = None
    theta: float | None = None            # per-share daily time decay (we collect it, short)
    gamma: float | None = None            # delta's rate of change (assignment-risk acceleration)
    vega: float | None = None             # sensitivity to a 1-pt IV move
    open_interest: int | None = None
    volume: int | None = None
    as_of: datetime = field(default_factory=utcnow)

    @property
    def midpoint(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return round((self.bid + self.ask) / 2, 4)
        return self.mark

    @property
    def spread_pct(self) -> float | None:
        """Bid-ask spread as a fraction of mid (None if not computable)."""
        mid = self.midpoint
        if mid is None or mid <= 0 or self.bid is None or self.ask is None:
            return None
        return (self.ask - self.bid) / mid

    @property
    def is_valid(self) -> bool:
        """Usable for pricing a sell-to-open: positive bid and ask present."""
        return self.bid is not None and self.ask is not None and self.bid > 0

    def age_seconds(self, now: datetime | None = None) -> float:
        now = now or utcnow()
        return (now - self.as_of).total_seconds()

    def is_stale(self, max_age_seconds: float, now: datetime | None = None) -> bool:
        return self.age_seconds(now) > max_age_seconds

    def dte(self, today: date | None = None) -> int:
        today = today or utcnow().date()
        return (self.expiration - today).days
