"""AI-written tactical analysis for the weekly brief.

Advisory and fail-open: any problem (no client, no key, API error) returns None and the brief renders
with just its deterministic sections. Reuses the reviewer's Anthropic client via a plain-text
completion (like ai/weekly.py). Descriptive market prep only — the prompt forbids buy/sell advice.

Feeds Claude the full high-signal picture already on the box: the market regime, this week's
catalysts, the operator's OWN outcome history (the analytics flywheel), current open book + risk
state (loss breaker, sector concentration), the names the scanner skipped today and why, enriched
per-name structural data (incl. IV-vs-realized), and advisory ideas across accounts.
"""
from __future__ import annotations

import json
import logging
import math

from ..services.brief_metrics import (
    assignment_capacity,
    earnings_before_expiry,
    position_management,
    strike_cushion,
)
from ..services.weekly_brief import _by_underlying, _short_put_pairs

log = logging.getLogger("agentic.ai.brief_analysis")

SYSTEM = (
    "You are the analyst for a cash-secured-put options-wheel bot, writing the operator's Monday "
    "tactical prep note (read on a phone). You are given: the market regime; this week's economic + "
    "earnings catalysts; the operator's OWN historical outcomes bucketed by feature ('your_history'); "
    "the current open book and risk state (loss breaker, sector concentration); the names the scanner "
    "SKIPPED today and why; enriched per-name structural data; and advisory ideas across accounts.\n\n"
    "Write 8-11 plain sentences (no markdown, no bullet points, no preamble or sign-off). LEAD with a "
    "ranked verdict: name the 1-3 best-set-up names this week and, in one clause each, WHY; then the "
    "1-2 worth standing aside on and why. Then support that verdict, prioritizing in this order for a "
    "7-14 DTE premium seller: (1) VOL RICHNESS — IV rank is the primary read on how rich premium is. "
    "iv_rv_ratio is the sold put's implied vs the name's realized vol; it is skewed high by put skew, "
    "so use it to CORROBORATE a rich IV-rank read, not as a standalone verdict. (2) CUSHION — each "
    "name carries 'expected_move' (the option-implied 1-sigma move over the contract) with 'strike_em' "
    "(how many expected-moves OTM the bot's strike sits) and 'support_below_strike_em' (extra cushion "
    "to structural support). Treat strike_em as the core structural read: >~1.0 is a comfortable "
    "cushion, <~0.7 is thin. (3) EVENT RISK — if 'earnings_inside_contract' is true, flag it loudly "
    "(a binary event lands before expiry); also weigh REGIME (VIX, SPY & QQQ vs 200-SMA, drawdown) and "
    "the week's key econ catalyst (FOMC/CPI) as event risk on short-dated contracts, and cite WHY the "
    "scanner skipped names. (4) BOOK RISK — 'assignment' shows collateral-if-all-assigned vs buying "
    "power (flag coverage < ~1.0 or any shortfall); 'management' lists open shorts near expiry / near "
    "or through their strike (call out roll_window and ITM names); plus sector concentration and "
    "whether the loss breaker is near tripping. (5) When 'your_history' has a bucket with enough "
    "trades (n >= ~15) you may cite it (e.g. 'your 0.25-0.30 delta puts have won X%'), but treat "
    "small-n buckets as weak and say so — never over-claim on a handful of trades.\n\n"
    "Reference the given numbers; never invent numbers beyond those provided. Distinguish a "
    "market-wide dip (a name's drawdown ~ SPY's drawdown = systemic) from a name-specific breakdown. "
    "Do NOT dwell on RSI / ADX / Bollinger %B (weak filters at this horizon), and never use sentiment "
    "scores, unusual-options-activity, or intraday price action. This is descriptive situational "
    "awareness, NOT financial advice: describe setups, cushions, and what to watch, and name risks "
    "plainly — never tell the operator to buy or sell."
)


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _flywheel_slim(fw: dict | None) -> dict | None:
    """The operator's own outcome history: overall summary + buckets for the dimensions that matter
    most to a premium seller. Keeps buckets with n>=3 (small-n flagged by the prompt)."""
    if not fw:
        return None
    keep = ("delta", "iv_rank", "dte", "iv_rv_ratio", "quality_score", "mkt_regime", "exit_reason")
    by = fw.get("by_feature", {}) or {}
    slim = {d: [b for b in by.get(d, []) if (b.get("n") or 0) >= 3] for d in keep if d in by}
    return {"summary": fw.get("summary", {}), "by_feature": {k: v for k, v in slim.items() if v}}


def _accounts_slim(accounts: list | None) -> list:
    out = []
    for a in accounts or []:
        if "error" in a:
            continue
        out.append({
            "account": a.get("account_number"), "value": a.get("account_value"),
            "buying_power": a.get("buying_power"), "weekly_target": a.get("weekly_target"),
            "holdings": [{"symbol": h.get("symbol"), "shares": h.get("shares")}
                         for h in (a.get("holdings") or [])[:8]],
            "top_cc": [{"sym": c.get("underlying"), "strike": c.get("strike"),
                        "wk": c.get("weekly_dollars")} for c in (a.get("covered_calls") or [])[:2]],
            "top_csp": [{"sym": c.get("underlying"), "strike": c.get("strike"),
                         "wk": c.get("weekly_dollars")} for c in (a.get("cash_secured_puts") or [])[:2]],
        })
    return out


def _reviews_slim(reviews: list | None) -> list:
    return [{"symbol": r.get("underlying"), "recommendation": r.get("recommendation"),
             "confidence": r.get("confidence"), "flags": r.get("flags"),
             "move_class": r.get("move_class")} for r in (reviews or [])[:8]]


async def generate_brief_analysis(
    client, *, watchlist: list[str], contexts: dict, candidates: list[dict],
    tv_by_symbol: dict, regime: dict | None, econ_events: list,
    flywheel: dict | None = None, skips: list | None = None, accounts: list | None = None,
    stats: dict | None = None, risk: dict | None = None, ai_reviews: list | None = None,
    open_positions: list | None = None, total_buying_power: float | None = None,
) -> str | None:
    """Return a short prose tactical read, or None on any error / missing client."""
    if client is None or not hasattr(client, "summarize"):
        return None
    cand_by_u = _by_underlying(candidates)
    spy_dd = (regime or {}).get("spy_drawdown_20d")
    names = []
    for sym in watchlist:
        ctx = contexts.get(sym) or {}
        tv = tv_by_symbol.get(sym) or {}
        cd = cand_by_u.get(sym) or {}
        iv, rv = cd.get("iv"), ctx.get("realized_vol")
        iv_rv = round(iv / rv, 2) if (_num(iv) and _num(rv) and rv) else None
        dd = ctx.get("drawdown_20d")
        systemic = None
        if _num(dd) and _num(spy_dd):
            systemic = abs(dd - spy_dd) < 0.04  # name falling roughly with the market
        cush = strike_cushion(price=ctx.get("price"), strike=cd.get("strike"), iv=iv,
                              dte=cd.get("dte"), support=tv.get("support"))
        earn_inside = earnings_before_expiry(ctx.get("days_to_earnings"), cd.get("dte"))
        names.append({
            "symbol": sym, "price": ctx.get("price"), "sma50": ctx.get("sma50"),
            "sma200": ctx.get("sma200"), "above_sma200": ctx.get("above_sma200"),
            "adx": ctx.get("adx"), "rsi": ctx.get("rsi"), "atr": ctx.get("atr"),
            "realized_vol": rv, "drawdown_20d": dd, "systemic_dip": systemic,
            "iv_rank": ctx.get("iv_rank"), "iv_rv_ratio": iv_rv,
            "quality_score": ctx.get("quality_score"), "days_to_earnings": ctx.get("days_to_earnings"),
            "earnings_inside_contract": earn_inside,
            "recent_news_count": ctx.get("recent_news_count"),
            "support": tv.get("support"), "resistance": tv.get("resistance"),
            "bot_strike": cd.get("strike"), "bot_delta": cd.get("delta"), "bot_dte": cd.get("dte"),
            "bot_credit": cd.get("premium"), "bot_annualized_ror": cd.get("annualized_ror"),
            "bot_break_even": cd.get("break_even"),
            "expected_move": cush["expected_move"], "strike_em": cush["strike_em"],
            "support_em": cush["support_em"], "support_below_strike_em": cush["support_below_strike_em"],
        })
    prices = {s: (c or {}).get("price") for s, c in (contexts or {}).items()}
    atrs = {s: (c or {}).get("atr") for s, c in (contexts or {}).items()}
    management = position_management(open_positions or [], prices, atrs)
    assignment = assignment_capacity(_short_put_pairs(open_positions), total_buying_power)
    payload = {
        "market": regime or {},
        "econ_events_next_7d": [
            {"date": str(getattr(e, "date", "")), "event": getattr(e, "event", ""),
             "importance": getattr(e, "importance", "")} for e in (econ_events or [])],
        "your_history": _flywheel_slim(flywheel),
        "open_book": {k: (stats or {}).get(k) for k in (
            "open_count", "resolved_count", "win_rate", "realized_pnl", "unrealized_pnl",
            "credit_collected_resolved", "by_rule")} if stats else None,
        "risk": risk or None,
        "assignment": assignment,
        "management": management,
        "skipped_today": [{"symbol": s.get("symbol"), "reason": s.get("reason")}
                          for s in (skips or [])],
        "watchlist": names,
        "accounts": _accounts_slim(accounts),
        "ai_reviews": _reviews_slim(ai_reviews),
    }
    user = "Brief data (JSON):\n" + json.dumps(payload, default=str) + "\n\nWrite the tactical prep note."
    try:
        text = await client.summarize(SYSTEM, user, max_tokens=800)
        return text or None
    except Exception as exc:  # noqa: BLE001 — advisory; the brief renders without it
        log.warning("Brief AI analysis generation failed: %s", exc)
        return None
