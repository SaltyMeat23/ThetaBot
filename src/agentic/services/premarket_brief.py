"""On-demand pre-market watchlist brief — decision support, never auto-entry.

reporting.py covers a daily heartbeat + weekly rollup but there is no pre-open review. This assembles
one human-facing markdown brief per watchlist name from (chart values read via the TradingView MCP)
+ (the bot's own API). It surfaces trend STATE (a label, not a trade call), structural S/R beside the
bot's stored values (annotated with feed divergences via tv_reconcile), the distance-to-support that
matters for strike selection, and feed freshness — so the operator can tune the watchlist / per-ticker
criteria. Pure builder (testable); a thin tools driver does the I/O.
"""
from __future__ import annotations

from typing import Any

from ..tools.tv_reconcile import ReconcileTolerance, reconcile_symbol


def _adx_bucket(adx: float | None) -> str:
    if adx is None:
        return "ADX n/a"
    if adx < 20:
        return f"ADX {adx:.0f} (weak/choppy)"
    if adx < 25:
        return f"ADX {adx:.0f} (developing)"
    return f"ADX {adx:.0f} (trending)"


def _trend_label(chart: dict[str, Any]) -> str:
    price = chart.get("price")
    sma200 = chart.get("sma200")
    dist = chart.get("dist_sma200_pct")
    if price is not None and sma200:
        rel = "above" if price >= sma200 else "below"
        pct = (price - sma200) / sma200 * 100
        base = f"{rel} 200-SMA ({pct:+.1f}%)"
    elif dist is not None:
        base = f"{'above' if dist >= 0 else 'below'} 200-SMA ({dist:+.1f}%)"
    else:
        base = "200-SMA n/a"
    return f"{base} · {_adx_bucket(chart.get('adx'))}"


def _effective_criteria(bot_config: dict, symbol: str) -> dict:
    entry = (bot_config.get("editable") or {}).get("entry") or {}
    base = dict(entry.get("criteria") or {})
    per_ticker = entry.get("per_ticker") or {}
    override = per_ticker.get(symbol.upper()) or per_ticker.get(symbol) or {}
    base.update(override)
    return base


def _freshness(tv_health: dict, symbol: str) -> tuple[str, bool]:
    for s in tv_health.get("symbols", []):
        if str(s.get("symbol", "")).upper() == symbol.upper():
            if not s.get("present"):
                return "missing from feed", True
            if s.get("stale"):
                return f"STALE ({s.get('age_seconds', 0):.0f}s)", True
            return f"fresh ({s.get('age_seconds', 0):.0f}s)", False
    return "missing from feed", True


def _symbol_block(
    symbol: str, chart: dict[str, Any], bot_recent: list[dict], tv_health: dict,
    bot_config: dict, max_age_seconds: int,
) -> str:
    price = chart.get("price")
    support = chart.get("support")
    resistance = chart.get("resistance")
    crit = _effective_criteria(bot_config, symbol)

    lines = [f"### {symbol.upper()}", f"- **Trend:** {_trend_label(chart)}"]

    # Structural S/R (chart) + bot's stored values, with a divergence annotation.
    sr = f"- **Structural S/R:** support {support if support is not None else 'n/a'}"
    sr += f" · resistance {resistance if resistance is not None else 'n/a'}"
    lines.append(sr)

    row = next((r for r in bot_recent if str(r.get("symbol", "")).upper() == symbol.upper()), None)
    rep = reconcile_symbol(
        symbol, chart, row.get("payload") if row else None,
        row.get("age_seconds") if row else None,
        tol=ReconcileTolerance(stale_after_seconds=max_age_seconds),
    )
    if not rep.ok:
        issues = "; ".join(f"{d.field}:{d.kind}" for d in rep.divergences) or "stale/absent"
        lines.append(f"- **⚠ Feed divergence:** {issues}")

    # Distance-to-support that matters for CSP strike placement.
    if price is not None and support:
        buf = (price - support) / support * 100
        dband = f"δ {crit.get('delta_min', '?')}–{crit.get('delta_max', '?')}"
        gate = "ON" if crit.get("require_strike_below_support") else "off"
        lines.append(
            f"- **Strike room:** price {price:g} is {buf:+.1f}% vs support {support:g} · "
            f"{dband} · support-gate {gate}"
        )

    fresh, warn = _freshness(tv_health, symbol)
    lines.append(f"- **Feed:** {'⚠ ' if warn else ''}{fresh}")
    return "\n".join(lines)


def build_premarket_brief(
    *, watchlist_name: str, symbols: list[str], chart_by_symbol: dict[str, dict],
    bot_indicators: list[dict], tv_health: dict, bot_config: dict,
    max_age_seconds: int | None = None,
) -> tuple[str, str]:
    """Return (title, markdown). Decision support only — states, not trade recommendations."""
    max_age = max_age_seconds or tv_health.get("threshold_seconds") or 108_000
    title = f"Pre-market brief · {watchlist_name} · {len(symbols)} names"
    body = [f"# {title}", "", "_Chart context to inform watchlist / per-ticker tuning — not trade "
            "recommendations, not investment advice._", ""]
    for sym in symbols:
        chart = chart_by_symbol.get(sym.upper()) or chart_by_symbol.get(sym) or {}
        body.append(_symbol_block(sym, chart, bot_indicators, tv_health, bot_config, max_age))
        body.append("")
    stale = [s.get("symbol") for s in tv_health.get("symbols", []) if s.get("stale") or not s.get("present")]
    if stale:
        body.append(f"> ⚠ Feed stale/missing for: {', '.join(str(x) for x in stale)}")
    return title, "\n".join(body)
