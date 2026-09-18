"""Share-holding helpers: which holdings are tradable (not the tax reserve), how long shares have
been held since assignment, and whether the assignment clock allows calls below cost basis. Pure."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


def reserve_symbols(settings) -> set[str]:
    cfg = getattr(settings, "tax_reserve", None)
    return {str(cfg.symbol).upper()} if cfg is not None and getattr(cfg, "symbol", None) else set()


def tradable_holdings(holdings: Iterable[Any], reserve: set[str]) -> list[Any]:
    """Holdings the wheel may write calls on / count as assignment evidence."""
    return [h for h in holdings if str(getattr(h, "symbol", "")).upper() not in reserve]


def held_since(journal_entries: Iterable[Any], symbol: str) -> datetime | None:
    """When the shares arrived: ``closed_at`` of the latest ASSIGNED journal row for the underlying.
    None when unknown (shares that predate the journal) -> the caller keeps the basis floor."""
    sym = symbol.upper()
    best: datetime | None = None
    for e in journal_entries:
        if str(getattr(e, "underlying", "")).upper() != sym or getattr(e, "status", None) != "assigned":
            continue
        ts = getattr(e, "closed_at", None)
        if isinstance(ts, str):
            try:
                ts = datetime.fromisoformat(ts)
            except ValueError:
                continue
        if ts is None:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if best is None or ts > best:
            best = ts
    return best


def days_held(journal_entries: Iterable[Any], symbol: str, now: datetime) -> int | None:
    since = held_since(journal_entries, symbol)
    if since is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return max(0, (now - since).days)


def below_basis_allowed(holding, criteria, journal_entries: Iterable[Any], now: datetime,
                        price: float | None) -> tuple[bool, int | None]:
    """(allowed, days_held). Allowed only when the criteria set a clock, the shares are under water,
    and they have been held at least that many days since assignment."""
    n = getattr(criteria, "cc_below_basis_after_days", None)
    if not n:
        return False, None
    d = days_held(journal_entries, holding.symbol, now)
    if d is None:
        return False, None
    under = price is not None and price < float(getattr(holding, "average_cost", 0.0) or 0.0)
    return (under and d >= int(n)), d
