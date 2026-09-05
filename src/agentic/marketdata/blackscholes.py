"""Black-Scholes pricing + implied-volatility solver (pure, tested).

Used to reconstruct historical implied volatility from option *prices* (Alpaca gives historical
option bars but no greeks/IV), which seeds IV Rank and underpins the future backtest. European
BS, no dividends, constant rate — a proxy, good enough for a volatility-regime signal on the
liquid names we trade. Returns None when a price can't be inverted (below intrinsic, etc.).
"""
from __future__ import annotations

import math

DEFAULT_RATE = 0.045  # constant risk-free proxy; rate barely moves short-dated IV


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes price of a European option."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        # Degenerate -> intrinsic value.
        return max(0.0, (S - K) if is_call else (K - S))
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_delta(S: float, K: float, T: float, r: float, sigma: float, is_call: bool) -> float:
    """Black-Scholes delta. Put delta is negative (N(d1)-1)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if is_call:
            return 1.0 if S > K else 0.0
        return -1.0 if S < K else 0.0
    d1 = (math.log(S / K) + (r + sigma * sigma / 2.0) * T) / (sigma * math.sqrt(T))
    return _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0


def implied_vol(
    price: float, S: float, K: float, T: float, r: float, is_call: bool,
    *, lo: float = 1e-3, hi: float = 5.0, tol: float = 1e-4, max_iter: int = 100,
) -> float | None:
    """Invert BS for implied volatility via bisection (price is monotincreasing in sigma).

    Returns None if the price is outside the arbitrage bounds (e.g. below intrinsic) so it
    can't be inverted, or inputs are degenerate.
    """
    if price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return None
    intrinsic = max(0.0, (S - K) if is_call else (K - S))
    if price < intrinsic - tol:
        return None  # below intrinsic -> not invertible
    lo_price = bs_price(S, K, T, r, lo, is_call)
    hi_price = bs_price(S, K, T, r, hi, is_call)
    if not (lo_price - tol <= price <= hi_price + tol):
        return None  # outside solvable range
    for _ in range(max_iter):
        mid = (lo + hi) / 2.0
        mid_price = bs_price(S, K, T, r, mid, is_call)
        if abs(mid_price - price) < tol:
            return round(mid, 4)
        if mid_price < price:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2.0, 4)
