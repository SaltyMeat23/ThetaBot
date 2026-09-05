"""Best-effort US equity-options market-hours check (regular session).

Used only to pick a polling cadence (faster when open, slower when closed). Not a
trading gate. Falls back to "open" if timezone data is unavailable so we never under-poll.
Does not account for market holidays.
"""
from __future__ import annotations

from datetime import datetime, time, timezone


def is_market_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        et = now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — missing tzdata on some Windows installs
        return True
    if et.weekday() >= 5:  # Sat/Sun
        return False
    return time(9, 30) <= et.time() <= time(16, 0)
