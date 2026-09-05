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
