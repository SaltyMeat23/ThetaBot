"""Advisory "what could I sell" suggestions for one Robinhood account — READ-ONLY.

For a given account it reads holdings + cash (never places orders) and returns, reusing the same
screener/cost-basis logic the bot uses:
  - covered calls on shares held (strikes at/above cost basis, sized to coverable shares), and
  - cash-secured puts on the watchlist, sized to the account's buying power.
Each suggestion carries a weekly-equivalent yield and dollar figure, so the calculator can compare
against the account's weekly premium target. This is advisory only — the bot trades just the
ring-fenced Agentic account; suggestions for other accounts are placed manually by the user.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from ..domain.enums import Direction, OptionType
from ..entry.screener import EntryCandidate, screen_candidates
from .screening import screen_universe

log = logging.getLogger("agentic.account_options")


def _weekly_yield_pct(c: EntryCandidate) -> float:
    """Weekly-equivalent yield on collateral: (premium/strike) annualized to a 7-day window."""
    if c.strike > 0 and c.dte > 0:
        return (c.premium / c.strike) * (7.0 / c.dte) * 100.0
    return 0.0


def _advisory_criteria(base):
    """Loosen a screening criteria for the ADVISORY calculator — it should surface the menu of what
    you *could* sell, wider than the bot's strict auto-trade filter, and let the user judge."""
    return base.model_copy(update={
        "delta_min": 0.10, "delta_max": 0.45, "min_annualized_yield": 0.05,
        "min_open_interest": 1, "min_volume": 1, "max_spread_pct": 0.35,
        # advisory only — don't apply the underlying trend/earnings gates here
        "require_above_sma200": False, "max_pct_below_sma200": None,
        "min_adx": None, "min_bb_percent_b": None, "rsi_min": None, "rsi_max": None,
        "require_strike_below_support": False,
    })


def _suggestion(c: EntryCandidate, contracts: int, *, kind: str,
                below_basis: bool | None = None) -> dict[str, Any]:
    weekly_dollars = (c.premium * 100 * contracts * (7.0 / c.dte)) if c.dte > 0 else 0.0
    out = {
        "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
        "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta, "iv": c.iv,
        "premium": c.premium, "annualized_ror": c.annualized_ror,
        "weekly_yield_pct": round(_weekly_yield_pct(c), 3),
        "contracts": contracts,
        "collateral": round(c.strike * 100 * contracts, 2) if kind == "csp" else 0.0,
        "weekly_dollars": round(weekly_dollars, 2),
    }
    if below_basis is not None:
        out["below_basis"] = below_basis
    return out


async def account_option_suggestions(
    broker, market_data, settings: Settings, account_number: str,
    *, per_name: int = 3, csp_candidates: list[EntryCandidate] | None = None,
) -> dict[str, Any]:
    """One account's advisory CC + CSP suggestions. ``csp_candidates`` lets the caller pre-screen the
    watchlist puts once and reuse them across accounts (they differ only by affordability)."""
    holdings = await broker.get_equity_positions(account_number)
    buying_power = await broker.get_buying_power(account_number)
    account_value = await broker.get_account_value(account_number)

    # existing short calls per underlying, so we never suggest covering shares already written
    short_calls: dict[str, int] = {}
    try:
        for p in await broker.get_open_positions(account_number):
            if p.direction is Direction.SHORT and p.option_type is OptionType.CALL:
                short_calls[p.underlying] = short_calls.get(p.underlying, 0) + p.quantity
    except Exception as exc:  # noqa: BLE001 — advisory; never break on a positions read
        log.warning("short-call read failed for %s: %s", account_number, exc)

    # covered calls on held shares. ADVISORY: no cost-basis floor — the delta band keeps strikes
    # out-of-the-money (above spot), and a below-cost-basis call is a valid way to collect premium
    # and lower the effective cost basis (esp. in a non-taxable account). Each suggestion is flagged
    # ``below_basis`` so the user sees which ones sit under their original cost.
    cc_crit = _advisory_criteria(settings.entry.cc_criteria)
    covered_calls: list[dict[str, Any]] = []
    holdings_out: list[dict[str, Any]] = []
    chain_cache: dict[str, Any] = {}
    for h in holdings:
        coverable = h.quantity // 100 - short_calls.get(h.symbol, 0)
        holdings_out.append({"symbol": h.symbol, "shares": h.quantity,
                             "cost_basis": round(h.average_cost, 4), "coverable": max(0, coverable)})
        if h.quantity < 100 or coverable < 1:
            continue
        try:
            chain = chain_cache.get(h.symbol)
            if chain is None:
                chain = await market_data.get_chain(h.symbol)
                chain_cache[h.symbol] = chain
        except Exception as exc:  # noqa: BLE001
            log.warning("chain fetch failed for %s: %s", h.symbol, exc)
            continue
        cands = screen_candidates(h.symbol, chain, cc_crit, option_type="call")
        for c in cands[:per_name]:
            covered_calls.append(_suggestion(
                c, coverable, kind="cc", below_basis=c.strike < h.average_cost))

    # cash-secured puts on the watchlist, sized to this account's buying power
    put_cands = csp_candidates
    if put_cands is None:
        try:
            put_cands = await screen_universe(
                market_data, list(settings.entry.watchlist),
                _advisory_criteria(settings.entry.criteria), limit=60)
        except Exception as exc:  # noqa: BLE001
            log.warning("CSP screen failed: %s", exc)
            put_cands = []
    cash_secured_puts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for c in put_cands:
        if c.underlying in seen or c.strike <= 0:
            continue
        contracts = int(buying_power // (c.strike * 100))
        if contracts < 1:
            continue
        seen.add(c.underlying)
        cash_secured_puts.append(_suggestion(c, contracts, kind="csp"))

    covered_calls.sort(key=lambda x: x["weekly_dollars"], reverse=True)
    cash_secured_puts.sort(key=lambda x: x["weekly_dollars"], reverse=True)
    wk_pct = settings.entry.weekly_premium_target_pct
    return {
        "account_number": account_number,
        "buying_power": round(buying_power, 2),
        "account_value": round(account_value, 2),
        "weekly_target_pct": wk_pct,
        "weekly_target": round(account_value * wk_pct, 2) if wk_pct else 0.0,
        "holdings": holdings_out,
        "covered_calls": covered_calls,
        "cash_secured_puts": cash_secured_puts,
    }
