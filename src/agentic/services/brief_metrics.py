"""Decision-oriented metrics for the weekly tactical brief.

Pure functions (no I/O) that turn the raw numbers already on the box into the reads a premium
seller actually decides on: the option-implied expected move and how much cushion the bot's strike
and the structural support level have relative to it; whether a known earnings date lands INSIDE the
contract (binary event risk on a short-dated short); how much short-put assignment exposure the book
carries vs available cash; and which OPEN positions need managing this week (near expiry / near the
strike). Descriptive only — none of this is a buy/sell instruction.
"""
from __future__ import annotations

import math
from typing import Any, Iterable


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def expected_move(price: float | None, iv: float | None, dte: float | None) -> float | None:
    """Option-implied 1-sigma move (in price units) over ``dte`` calendar days:
    ``price * iv * sqrt(dte/365)``. ``iv`` is an annualized fraction (0.55 = 55%). Returns None when
    any input is missing or non-positive."""
    if not (_num(price) and _num(iv) and _num(dte)) or price <= 0 or iv <= 0 or dte <= 0:
        return None
    return price * iv * math.sqrt(dte / 365.0)


def strike_cushion(
    *, price: float | None, strike: float | None, iv: float | None, dte: float | None,
    support: float | None = None,
) -> dict:
    """Express a short PUT's strike (and an optional structural support level) as multiples of the
    expected move — the single read that unifies IV, DTE, strike distance, and structure.

    Returns ``{expected_move, strike_em, support_em, support_below_strike_em}``:
      * ``strike_em``  — how many expected-moves below spot the strike sits (bigger = more cushion).
      * ``support_em`` — how many expected-moves below spot the support level sits.
      * ``support_below_strike_em`` — extra cushion: support's distance below the strike, in EMs.
    Fields are None where inputs are missing.
    """
    em = expected_move(price, iv, dte)
    out: dict = {"expected_move": round(em, 2) if em else None,
                 "strike_em": None, "support_em": None, "support_below_strike_em": None}
    if em and _num(strike):
        out["strike_em"] = round((price - strike) / em, 2)
    if em and _num(support):
        out["support_em"] = round((price - support) / em, 2)
        if _num(strike):
            out["support_below_strike_em"] = round((strike - support) / em, 2)
    return out


def earnings_before_expiry(days_to_earnings: float | None, dte: float | None) -> bool | None:
    """True when a known earnings date falls on/before the contract's expiration — i.e. a binary
    event lands inside the trade (the #1 gap/assignment risk on a short-dated short). None when the
    earnings date is unknown."""
    if not (_num(days_to_earnings) and _num(dte)):
        return None
    return 0 <= days_to_earnings <= dte


def assignment_capacity(open_puts: Iterable[tuple], buying_power: float | None) -> dict:
    """Aggregate short-PUT assignment exposure vs available cash across the book.

    ``open_puts``: iterable of ``(strike, quantity)`` for OPEN short puts. Returns
    ``{collateral, buying_power, coverage, shortfall}`` where ``collateral`` is the cash needed if
    every put were assigned (strike*100*qty), ``coverage`` = buying_power / collateral (None with no
    collateral), and ``shortfall`` = collateral - buying_power when under-covered (else 0)."""
    collateral = round(sum((s or 0) * 100 * (q or 0) for s, q in open_puts), 2)
    bp = round(buying_power, 2) if _num(buying_power) else None
    coverage = round(bp / collateral, 2) if (bp is not None and collateral > 0) else None
    shortfall = round(collateral - bp, 2) if (bp is not None and collateral > bp) else 0.0
    return {"collateral": collateral, "buying_power": bp,
            "coverage": coverage, "shortfall": shortfall}


def _opt_type(pos: Any) -> str:
    ot = getattr(pos, "option_type", None)
    return str(getattr(ot, "value", ot)).lower()


def _status(pos: Any) -> str:
    st = getattr(pos, "status", None)
    return str(getattr(st, "value", st)).upper()


def position_management(
    open_positions: Iterable[Any], price_by_symbol: dict[str, float] | None = None,
    atr_by_symbol: dict[str, float] | None = None, *, roll_dte: int = 21,
) -> list[dict]:
    """Per-open-position management read: DTE, a roll-window flag (dte <= ``roll_dte``, the standard
    ~21-DTE management point), and the short option's moneyness vs the underlying — ITM, near-the-
    money (within ~1 ATR of the strike), or OTM — when a current price is known.

    Pure: the caller supplies current underlying prices (and optional ATRs); positions without a
    known price still report DTE + roll flag. Only OPEN/CLOSING short options are included. Sorted by
    DTE ascending (soonest to manage first)."""
    prices = price_by_symbol or {}
    atrs = atr_by_symbol or {}
    out: list[dict] = []
    for p in open_positions:
        if _status(p) not in ("OPEN", "CLOSING"):
            continue
        ot = _opt_type(p)
        if ot not in ("put", "call"):
            continue
        sym = getattr(p, "underlying", None)
        strike = getattr(p, "strike", None)
        try:
            dte = p.dte()
        except Exception:  # noqa: BLE001 — a missing expiration should not sink the section
            dte = None
        price = prices.get(sym)
        atr = atrs.get(sym)
        moneyness = None
        cushion_atr = None
        if _num(price) and _num(strike):
            # cushion = distance the short strike sits OUT of the money (positive = safe side).
            cushion = (price - strike) if ot == "put" else (strike - price)
            if cushion < 0:
                moneyness = "ITM"
            elif _num(atr) and atr > 0 and cushion <= atr:
                moneyness = "near"
            else:
                moneyness = "OTM"
            if _num(atr) and atr > 0:
                cushion_atr = round(cushion / atr, 2)
        out.append({
            "symbol": sym, "option_type": ot, "strike": strike,
            "quantity": getattr(p, "quantity", None), "dte": dte,
            "roll_window": (dte is not None and dte <= roll_dte),
            "moneyness": moneyness, "price": price, "cushion_atr": cushion_atr,
        })
    out.sort(key=lambda r: (r["dte"] is None, r["dte"] if r["dte"] is not None else 0))
    return out
