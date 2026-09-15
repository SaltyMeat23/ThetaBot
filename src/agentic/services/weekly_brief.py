"""Weekly tactical prep brief — a dashboard-only, on-demand markdown report.

Assembles, from data already on the box: the market backdrop (regime/VIX), this week's catalysts
(curated econ calendar + per-name earnings), per-watchlist prep (structural levels, trend, IV rank,
and the bot's own MECHANICAL rule-based strike target), and the broader Robinhood book (per-account
holdings + the advisory CC/CSP ideas account_options.py generates).

Descriptive/informational ONLY — structural levels, dates, IV context, and what the bot's rules are
mechanically eyeing. Never a buy/sell recommendation. Pure builder (testable); the dashboard endpoint
gathers the inputs and calls build_weekly_brief.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Any

from .brief_metrics import (
    assignment_capacity,
    earnings_before_expiry,
    position_management,
    strike_cushion,
)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _short_put_pairs(open_positions) -> list[tuple]:
    """(strike, quantity) for every OPEN/CLOSING short put — the assignment-capacity inputs."""
    pairs = []
    for p in open_positions or []:
        ot = getattr(p, "option_type", None)
        st = getattr(p, "status", None)
        if (str(getattr(ot, "value", ot)).lower() == "put"
                and str(getattr(st, "value", st)).upper() in ("OPEN", "CLOSING")):
            pairs.append((getattr(p, "strike", 0), getattr(p, "quantity", 0)))
    return pairs


def _trend(price, sma200, adx) -> str:
    parts = []
    if _num(price) and _num(sma200) and sma200:
        pct = (price - sma200) / sma200 * 100
        parts.append(f"{'above' if price >= sma200 else 'below'} 200-SMA ({pct:+.1f}%)")
    else:
        parts.append("200-SMA n/a")
    if _num(adx):
        state = "trending" if adx >= 25 else ("weak/choppy" if adx < 20 else "developing")
        parts.append(f"ADX {adx:.0f} ({state})")
    return " | ".join(parts)


def _by_underlying(candidates: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for c in candidates or []:
        u = c.get("underlying")
        if u and u not in out:  # candidates are pre-sorted; keep the top pick per name
            out[u] = c
    return out


def _backdrop(regime: dict | None) -> list[str]:
    lines = ["## Market backdrop", ""]
    if not regime:
        lines += ["- Regime data unavailable (the scanner has not computed it yet).", ""]
        return lines
    label = regime.get("label", "unknown")
    lines.append(f"- Regime: **{label}**" + ("  (RISK-OFF)" if regime.get("risk_off") else ""))
    vix, vs = regime.get("vix"), regime.get("vix_state")
    lines.append(f"- VIX: {vix:.1f} ({vs})" if _num(vix) else "- VIX: n/a")
    spy = "above" if regime.get("spy_above_sma200") else "below"
    qqq = "above" if regime.get("qqq_above_sma200") else "below"
    lines.append(f"- SPY {spy} 200-SMA | QQQ {qqq} 200-SMA")
    dd = regime.get("spy_drawdown_20d")
    if _num(dd):
        lines.append(f"- SPY 20-day drawdown: {dd * 100:+.1f}%")
    lines.append("")
    return lines


def _catalysts(watchlist, contexts, econ_events) -> list[str]:
    lines = ["## This week's catalysts", "", "**Economic events (next 7 days):**"]
    if econ_events:
        for e in econ_events:
            lines.append(f"- {e.date:%a %b %d} - {e.event} ({e.importance})")
    else:
        lines.append("- None in the curated calendar for the next 7 days.")
    lines.append("")
    earns = []
    for sym in watchlist:
        dte = (contexts.get(sym) or {}).get("days_to_earnings")
        if _num(dte) and 0 <= dte <= 14:
            earns.append((int(dte), sym))
    earns.sort()
    if earns:
        lines.append("**Earnings within ~2 weeks:**")
        lines += [f"- {sym}: in ~{dte}d" for dte, sym in earns]
    else:
        lines.append("**Earnings:** none within ~2 weeks on the watchlist.")
    lines.append("")
    return lines


def _watchlist(watchlist, contexts, cand_by_u, tv_by_symbol, news_by_symbol) -> list[str]:
    lines = ["## Watchlist prep", ""]
    for sym in watchlist:
        ctx = contexts.get(sym) or {}
        tv = tv_by_symbol.get(sym) or {}
        lines.append(f"### {sym}")
        price = ctx.get("price")
        lines.append(f"- **Trend:** {_trend(price, ctx.get('sma200'), ctx.get('adx'))}")
        sup, res = tv.get("support"), tv.get("resistance")
        if sup is not None or res is not None:
            lvl = f"- **Levels:** support {sup if sup is not None else 'n/a'} | resistance {res if res is not None else 'n/a'}"
            if _num(price):
                lvl += f" | price {price:g}"
            lines.append(lvl)
        ivr = ctx.get("iv_rank")
        if _num(ivr):
            lines.append(f"- **IV rank:** {ivr:.0f}" + (" (premium is rich)" if ivr >= 50 else " (premium is thin)"))
        cand = cand_by_u.get(sym)
        if cand and _num(cand.get("strike")):
            d = abs(cand["delta"]) if _num(cand.get("delta")) else None
            bits = f"${cand['strike']:g} put"
            if d is not None:
                bits += f" | {d:.2f} delta"
            if _num(cand.get("dte")):
                bits += f" | {int(cand['dte'])}d"
            if _num(cand.get("premium")):
                bits += f" | ~${cand['premium']:.2f} credit"
            lines.append(f"- **Bot's rule is eyeing:** {bits}  (mechanical screen, not a recommendation)")
            # Expected-move cushion: strike (and support) distance as multiples of the 1-sigma move.
            cush = strike_cushion(price=price, strike=cand.get("strike"), iv=cand.get("iv"),
                                  dte=cand.get("dte"), support=sup)
            if cush["strike_em"] is not None:
                cl = f"- **Cushion:** strike sits {cush['strike_em']:.1f}x the expected move OTM"
                if cush["support_below_strike_em"] is not None:
                    cl += f"; support {cush['support_below_strike_em']:.1f}x further below"
                cl += f"  (1-sigma ~${cush['expected_move']:g} over {int(cand['dte'])}d)"
                lines.append(cl)
        qs = ctx.get("quality_score")
        if _num(qs):
            lines.append(f"- **Quality score:** {qs:.0f}/100")
        dte = ctx.get("days_to_earnings")
        cand_dte = cand.get("dte") if cand else None
        if earnings_before_expiry(dte, cand_dte) is True:
            lines.append(f"- **Earnings INSIDE the contract:** reports in ~{int(dte)}d, before the "
                         f"{int(cand_dte)}d expiry - binary event risk on the short.")
        elif _num(dte) and 0 <= dte <= 14:
            lines.append(f"- **Heads up:** earnings in ~{int(dte)}d (assignment-through-earnings risk)")
        hl = news_by_symbol.get(sym)
        if hl:
            lines.append(f"- **News:** {hl}")
        lines.append("")
    return lines


def _management(open_positions, contexts, total_buying_power) -> list[str]:
    lines = ["## Position management", "",
             "_Open short options that may need attention this week - descriptive, not advice._", ""]
    pairs = _short_put_pairs(open_positions)
    cap = assignment_capacity(pairs, total_buying_power)
    if cap["collateral"] > 0:
        cl = f"- **Assignment capacity:** ${cap['collateral']:,.0f} needed if every short put is assigned"
        if cap["buying_power"] is not None:
            cov = f"{cap['coverage']:.2f}x" if cap["coverage"] is not None else "n/a"
            cl += f" vs ${cap['buying_power']:,.0f} buying power ({cov} coverage"
            cl += f"; ${cap['shortfall']:,.0f} short)" if cap["shortfall"] > 0 else ")"
        lines.append(cl)
    prices = {s: (c or {}).get("price") for s, c in (contexts or {}).items()}
    atrs = {s: (c or {}).get("atr") for s, c in (contexts or {}).items()}
    rows = position_management(open_positions or [], prices, atrs)
    if not rows:
        lines += ["- No open short options.", ""]
        return lines
    for r in rows:
        tags = []
        if r["roll_window"]:
            tags.append("in roll window (<=21 DTE)")
        if r["moneyness"] == "ITM":
            tags.append("ITM")
        elif r["moneyness"] == "near":
            tags.append("near the strike")
        tag = f" - {', '.join(tags)}" if tags else ""
        dte = f"{r['dte']}d" if r["dte"] is not None else "DTE n/a"
        lines.append(f"- {r['symbol']} ${r['strike']:g}{r['option_type'][0].upper()} | {dte}{tag}")
    lines.append("")
    return lines


def _portfolio(accounts) -> list[str]:
    lines = ["## Broader portfolio", "",
             "_Advisory 'what you could sell' across your accounts - read-only, not a recommendation._", ""]
    if not accounts:
        lines += ["- No account data available.", ""]
        return lines
    for a in accounts:
        num = a.get("account_number", "?")
        if "error" in a:
            lines += [f"### Account {num}", f"- unavailable ({a['error']})", ""]
            continue
        lines.append(f"### Account {num}")
        lines.append(
            f"- Value ${a.get('account_value', 0):,.0f} | buying power ${a.get('buying_power', 0):,.0f}"
            f" | weekly target ${a.get('weekly_target', 0):,.0f}")
        holds = a.get("holdings") or []
        if holds:
            top = ", ".join(f"{h['symbol']} x{h['shares']}" for h in holds[:8])
            lines.append(f"- Holdings: {top}")
        for c in (a.get("covered_calls") or [])[:3]:
            note = " (below cost basis)" if c.get("below_basis") else ""
            lines.append(f"- CC idea: {c['underlying']} ${c['strike']:g}C | {c['dte']}d | "
                         f"~${c['weekly_dollars']:.0f}/wk{note}")
        for c in (a.get("cash_secured_puts") or [])[:3]:
            lines.append(f"- CSP idea: {c['underlying']} ${c['strike']:g}P | {c['dte']}d | "
                         f"~${c['weekly_dollars']:.0f}/wk")
        lines.append("")
    return lines


def build_weekly_brief(
    *, watchlist: list[str], contexts: dict[str, dict], candidates: list[dict],
    tv_by_symbol: dict[str, dict], regime: dict | None, news_by_symbol: dict[str, str],
    accounts: list[dict], econ_events: list, now: datetime, ai_analysis: str | None = None,
    open_positions: list | None = None, total_buying_power: float | None = None,
) -> tuple[str, str]:
    """Return (title, markdown). Descriptive/informational only. ``ai_analysis`` is an optional
    Claude-written tactical synthesis rendered up top (fail-open — omitted when unavailable).
    ``open_positions`` (domain Position objects) + ``total_buying_power`` drive the position-
    management section (roll window, moneyness) and the assignment-capacity read."""
    title = f"Weekly tactical brief - {now:%a %b %d, %Y}"
    body = [
        f"# {title}", "",
        "_Tactical prep: structural levels, the bot's mechanical rule targets, and upcoming "
        "catalysts. Descriptive only - NOT financial advice and NOT a recommendation._", "",
    ]
    if ai_analysis:
        body += ["## Tactical read", "", "_AI synthesis of the data below - situational awareness, "
                 "not advice._", "", ai_analysis, ""]
    body += _backdrop(regime)
    body += _catalysts(watchlist, contexts, econ_events)
    body += _watchlist(watchlist, contexts, _by_underlying(candidates), tv_by_symbol, news_by_symbol)
    body += _management(open_positions, contexts, total_buying_power)
    body += _portfolio(accounts)
    return title, "\n".join(body)
