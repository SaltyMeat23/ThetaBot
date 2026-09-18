"""Pure technical-indicator + IV-Rank functions (no I/O; mirrors services/stats.py purity).

All series are chronological (oldest -> newest). Every function returns None when there isn't
enough data, so callers can treat "unknown" as "don't gate on it" rather than a failure.
"""
from __future__ import annotations

import math
import statistics

TRADING_DAYS = 252


def sma(values: list[float], n: int) -> float | None:
    if len(values) < n or n <= 0:
        return None
    return sum(values[-n:]) / n


def rsi(closes: list[float], n: int = 14) -> float | None:
    """Relative Strength Index over the last n periods (simple-average variant)."""
    if len(closes) < n + 1:
        return None
    gains, losses = 0.0, 0.0
    for prev, cur in zip(closes[-(n + 1):-1], closes[-n:]):
        change = cur - prev
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain, avg_loss = gains / n, losses / n
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def realized_vol(closes: list[float], n: int = 20) -> float | None:
    """Annualized realized volatility from the last n daily log returns (as a fraction)."""
    if len(closes) < n + 1:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-(n + 1):-1], closes[-n:]) if a > 0]
    if len(rets) < 2:
        return None
    return round(statistics.pstdev(rets) * math.sqrt(TRADING_DAYS), 4)


def atr(highs: list[float], lows: list[float], closes: list[float], n: int = 14) -> float | None:
    """Average True Range over the last n periods."""
    if len(closes) < n + 1 or len(highs) < n + 1 or len(lows) < n + 1:
        return None
    trs: list[float] = []
    for i in range(len(closes) - n, len(closes)):
        prev_close = closes[i - 1]
        trs.append(max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close)))
    return round(sum(trs) / n, 4)


def iv_rank(current_iv: float | None, history: list[float], min_days: int = 60) -> float | None:
    """IV Rank = (current - min) / (max - min) * 100 over the history window.

    Returns None when current IV is unknown or history is too short to be meaningful — so the
    IV-Rank *gate* stays inactive until enough daily IV has accumulated (or a backfill lands).
    """
    if current_iv is None or len(history) < min_days:
        return None
    lo, hi = min(history), max(history)
    if hi <= lo:
        return None
    return round((current_iv - lo) / (hi - lo) * 100, 1)


# --- setup-detection helpers (pure; None on insufficient data) ----------------------------------

def ema(values: list[float], n: int) -> float | None:
    """Exponential moving average, seeded with the SMA of the first n values."""
    if n <= 0 or len(values) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(values[:n]) / n
    for v in values[n:]:
        e = v * k + e * (1 - k)
    return e


def stdev(values: list[float], n: int) -> float | None:
    """Population standard deviation of the last n values."""
    if n <= 1 or len(values) < n:
        return None
    return statistics.pstdev(values[-n:])


def bollinger(closes: list[float], n: int = 20, k: float = 2.0) -> tuple[float, float, float] | None:
    """(lower, mid, upper) Bollinger Bands: SMA(n) +/- k * population stdev(n)."""
    mid, sd = sma(closes, n), stdev(closes, n)
    if mid is None or sd is None:
        return None
    return (mid - k * sd, mid, mid + k * sd)


def bb_percent_b(closes: list[float], n: int = 20, k: float = 2.0) -> float | None:
    """Bollinger %B on a 0-100 scale (matches the TradingView feed field): 0 = at the lower band,
    100 = at the upper band. None when the bands collapse to zero width."""
    bb = bollinger(closes, n, k)
    if bb is None:
        return None
    lo, _mid, hi = bb
    width = hi - lo
    if width <= 0:
        return None
    return round((closes[-1] - lo) / width * 100, 2)


def bb_width_pct(closes: list[float], n: int = 20, k: float = 2.0) -> float | None:
    """Bollinger band width as a % of the middle band: (upper - lower) / mid * 100."""
    bb = bollinger(closes, n, k)
    if bb is None:
        return None
    lo, mid, hi = bb
    if mid <= 0:
        return None
    return round((hi - lo) / mid * 100, 4)


def bb_width_series(closes: list[float], n: int = 20, k: float = 2.0,
                    lookback: int = 60) -> list[float]:
    """Trailing Bollinger widths (oldest -> newest), one per bar over the last ``lookback`` bars
    that have a full n-window; the LAST element is the current bar's width. Lets a caller test
    'width at its lowest of the trailing window' (a volatility squeeze) against the PRIOR widths."""
    out: list[float] = []
    start = max(n, len(closes) - lookback)
    for end in range(start, len(closes) + 1):
        w = bb_width_pct(closes[:end], n, k)
        if w is not None:
            out.append(w)
    return out


def keltner(highs: list[float], lows: list[float], closes: list[float], n: int = 20,
            mult: float = 1.5) -> tuple[float, float, float] | None:
    """(lower, mid, upper) Keltner Channel: EMA(n) of closes +/- mult * ATR(n)."""
    mid, a = ema(closes, n), atr(highs, lows, closes, n)
    if mid is None or a is None:
        return None
    return (mid - mult * a, mid, mid + mult * a)


def donchian(highs: list[float], lows: list[float], n: int = 20,
             exclude_last: bool = True) -> tuple[float, float] | None:
    """(low, high) Donchian channel over the prior n bars. With ``exclude_last`` (default) the
    LAST bar is left out, so a breakout test compares a bar against the range it did NOT help
    form -- no look-ahead."""
    hs = highs[:-1] if exclude_last else highs
    ls = lows[:-1] if exclude_last else lows
    if n <= 0 or len(hs) < n or len(ls) < n:
        return None
    return (min(ls[-n:]), max(hs[-n:]))


def volume_ratio(volumes: list, n: int = 20) -> float | None:
    """Last bar's volume / mean volume of the prior n bars. None when the last volume or ANY prior
    volume is missing/non-positive (e.g. a provider that omits volume) -- callers must treat that as
    'unknown', never as 'no spike'."""
    if n <= 0 or len(volumes) < n + 1:
        return None
    last, prior = volumes[-1], volumes[-(n + 1):-1]
    if last is None or last <= 0 or any(v is None or v <= 0 for v in prior):
        return None
    avg = sum(prior) / n
    return round(last / avg, 3) if avg > 0 else None
