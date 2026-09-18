"""Feed QA: reconcile what TradingView actually shows vs what the bot received on the webhook.

The bot's ``tv_health`` only checks age/presence — never whether the stored VALUES are right or even
under the right key. This closes that gap. The scanner reads exactly the keys in ``SCANNER_CONSUMED``
(``services/scanner.py`` ``_enrich_ctx_from_tv`` / ``_support_ceiling``); a renamed field is stored
and shown but the matching gate silently never fires (fail-open). This engine flags that, plus stale
lingering fields and numeric drift.

Split: the pure ``reconcile*`` functions take (chart values I read via the TradingView MCP) and (bot
values from ``GET /api/tv-indicators``) as plain dicts, so they are unit-testable with fixtures and
never touch MCP/HTTP. The ``__main__`` driver does the I/O and prints the MCP-read recipe.

Run:  python -m agentic.tools.tv_reconcile --api-base https://host --symbols F,SOFI
      (prints the chart reads to run, then re-run with --chart-json <file> to get the report)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Literal

# --- the contract: what the scanner actually consumes -------------------------------------------
# Keys a STRUCTURED GATE reads (scanner.py _enrich_ctx_from_tv reads adx/bb_percent_b;
# _support_ceiling reads support). Renaming any of these silently disables its gate.
SCANNER_CONSUMED = frozenset({"support", "adx", "bb_percent_b"})
# Ingested + surfaced (dashboard / AI free-text) but no structured gate consumes it.
SCANNER_CONTEXT = frozenset({"resistance"})
# The keys reconciliation checks for drift/naming (structural S/R + gated technicals).
EXPECTED_KEYS = SCANNER_CONSUMED | SCANNER_CONTEXT
# Setup flags (entry/setups.py real-time layer): the Daily exporter emits the un-prefixed keys and
# a NEW intraday "Setup Exporter" emits the i_-prefixed ones. Consumed by scanner._enrich_ctx_from_tv
# via setups.parse_tv_setups (freshness keyed on d_bar_time / i_bar_time). Kept separate from
# EXPECTED_KEYS so reconciliation drift checks are unchanged; the Pine contract test uses the union.
SETUP_CONSUMED = frozenset({
    "squeeze_on", "breakout", "breakdown", "vol_ratio_20", "d_bar_time",
    "i_tf", "i_bar_time", "i_breakout_attempt", "i_breakdown_attempt", "i_support_test", "i_vol_anomaly",
})

# Common variants an exporter might emit instead of the canonical key. Used to distinguish a
# genuinely missing value from one that arrived under a name the gate never reads.
KNOWN_ALIASES: dict[str, str] = {
    "percent_b": "bb_percent_b", "bbpercentb": "bb_percent_b", "bb_pct_b": "bb_percent_b",
    "bbpct": "bb_percent_b", "pctb": "bb_percent_b", "%b": "bb_percent_b",
    "sup": "support", "s": "support", "supp": "support",
    "res": "resistance", "r": "resistance", "resist": "resistance",
    "adx14": "adx", "adx_14": "adx",
}

# Fields whose tolerance is a percentage of value (price levels) vs an absolute band (indicators).
_PRICE_FIELDS = frozenset({"support", "resistance"})

DivergenceKind = Literal[
    "name_mismatch", "unconsumed_alias", "missing_on_bot",
    "lingering_on_bot", "numeric_drift", "type_mismatch",
]


@dataclass(frozen=True)
class ReconcileTolerance:
    price_pct_tol: float = 0.005   # 0.5% for support/resistance
    adx_abs_tol: float = 2.0
    bb_abs_tol: float = 5.0        # bb_percent_b is on a 0..100 scale
    stale_after_seconds: int | None = None  # None -> reconcile() supplies the live threshold

    def within(self, field_name: str, a: float, b: float) -> bool:
        if field_name in _PRICE_FIELDS:
            denom = abs(a) if a else 1.0
            return abs(a - b) / denom <= self.price_pct_tol
        tol = self.bb_abs_tol if field_name == "bb_percent_b" else self.adx_abs_tol
        return abs(a - b) <= tol


@dataclass(frozen=True)
class FieldDivergence:
    symbol: str
    field: str
    kind: DivergenceKind
    chart_value: Any
    bot_value: Any
    detail: str


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    divergences: list[FieldDivergence] = field(default_factory=list)
    bot_age_seconds: float | None = None
    stale: bool = False
    present_on_bot: bool = True

    @property
    def ok(self) -> bool:
        return not self.divergences and not self.stale and self.present_on_bot


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _alias_in_bot(canonical: str, bot: dict[str, Any]) -> str | None:
    """Return a bot key that is a known alias of ``canonical`` (and holds a value), else None."""
    for k, v in bot.items():
        if k in EXPECTED_KEYS:
            continue
        if KNOWN_ALIASES.get(str(k).lower()) == canonical and v is not None:
            return k
    return None


def reconcile_symbol(
    symbol: str,
    chart: dict[str, Any],
    bot: dict[str, Any] | None,
    bot_age_seconds: float | None,
    *,
    tol: ReconcileTolerance,
) -> SymbolReport:
    """Compare one symbol's live chart values against the bot's stored snapshot."""
    sym = symbol.upper()
    divs: list[FieldDivergence] = []
    stale = (
        tol.stale_after_seconds is not None
        and bot_age_seconds is not None
        and bot_age_seconds > tol.stale_after_seconds
    )
    if bot is None:
        for k in sorted(EXPECTED_KEYS):
            if chart.get(k) is not None:
                divs.append(FieldDivergence(sym, k, "missing_on_bot", chart.get(k), None,
                                            "no snapshot stored for this symbol"))
        return SymbolReport(sym, divs, bot_age_seconds, stale, present_on_bot=False)

    for k in sorted(EXPECTED_KEYS):
        cv, bv = chart.get(k), bot.get(k)
        chart_has, bot_has = cv is not None, bv is not None
        if chart_has and bot_has:
            if _is_number(cv) and _is_number(bv):
                if not tol.within(k, float(cv), float(bv)):
                    divs.append(FieldDivergence(sym, k, "numeric_drift", cv, bv,
                                                f"chart {cv} vs bot {bv} beyond tolerance"))
            elif _is_number(cv) and not _is_number(bv):
                divs.append(FieldDivergence(sym, k, "type_mismatch", cv, bv,
                                            f"bot stored {bv!r} ({type(bv).__name__}); scanner's "
                                            "numeric guard silently skips it"))
        elif chart_has and not bot_has:
            alias = _alias_in_bot(k, bot)
            if alias is not None:
                consumed = " (gate silently never fires)" if k in SCANNER_CONSUMED else ""
                divs.append(FieldDivergence(sym, k, "unconsumed_alias", cv, bot.get(alias),
                                            f"bot stored {alias!r} but the code reads {k!r}{consumed}"))
            else:
                divs.append(FieldDivergence(sym, k, "missing_on_bot", cv, None,
                                            f"chart has {k}={cv}; bot never received it"))
        elif bot_has and not chart_has:
            divs.append(FieldDivergence(sym, k, "lingering_on_bot", None, bv,
                                        f"bot still holds {k}={bv} but the chart no longer emits it "
                                        "(merged snapshot; value may be stale)"))
    return SymbolReport(sym, divs, bot_age_seconds, stale, present_on_bot=True)


def reconcile(
    chart_by_symbol: dict[str, dict[str, Any]],
    bot_recent: list[dict[str, Any]],
    *,
    tol: ReconcileTolerance,
    max_age_seconds: int,
) -> list[SymbolReport]:
    """Reconcile every symbol in ``chart_by_symbol`` against the raw /api/tv-indicators list.

    ``bot_recent`` items are ``{symbol, received_at, age_seconds, payload}`` (the shape
    ``TVIndicatorStore.recent()`` / ``GET /api/tv-indicators`` returns).
    """
    if tol.stale_after_seconds is None:
        tol = ReconcileTolerance(tol.price_pct_tol, tol.adx_abs_tol, tol.bb_abs_tol, max_age_seconds)
    by_sym: dict[str, dict[str, Any]] = {str(r.get("symbol", "")).upper(): r for r in bot_recent}
    reports: list[SymbolReport] = []
    for sym, chart in chart_by_symbol.items():
        row = by_sym.get(sym.upper())
        payload = row.get("payload") if row else None
        age = row.get("age_seconds") if row else None
        reports.append(reconcile_symbol(sym, chart, payload, age, tol=tol))
    return reports


def format_report(reports: list[SymbolReport]) -> str:
    lines: list[str] = []
    for r in reports:
        tag = "OK" if r.ok else "DIVERGENCE"
        age = f"{r.bot_age_seconds:.0f}s" if r.bot_age_seconds is not None else "—"
        note = " STALE" if r.stale else ("" if r.present_on_bot else " ABSENT-ON-BOT")
        lines.append(f"[{tag}] {r.symbol}  (bot age {age}){note}")
        for d in r.divergences:
            lines.append(f"    - {d.kind}: {d.field}: {d.detail}")
    return "\n".join(lines) or "(no symbols)"


# --- driver (I/O; not imported by tests) --------------------------------------------------------

def _recipe(symbols: list[str]) -> str:
    reads = (
        '  IMPORTANT: the "AgenticRobinhood Feature Exporter" alert runs on the DAILY timeframe, so\n'
        '  read on 1D (chart_set_timeframe D) — NOT 30m. The tf:"30" in the stored payload comes from\n'
        '  a separate 30m Support-Resistance-Channels alert; ignore it for these fields.\n'
        '  1. data_get_ohlcv(count>=20) on 1D -> support = lowest low(20 days), resistance = highest high(20 days)\n'
        '  2. data_get_study_values on 1D     -> adx, bb_percent_b (daily; add "Average Directional\n'
        '     Index" and a Bollinger %B study if not present, or compute from daily bars)\n'
        '  3. quote_get                       -> last price (for sanity-ranging)\n'
    )
    return (
        "Run these TradingView MCP reads for each symbol, then re-run with --chart-json <file>\n"
        "where <file> is JSON: {\"F\": {\"support\": 9.55, \"resistance\": 10.19, \"adx\": 22.5,\n"
        "\"bb_percent_b\": 48.1}, ...}\n\nSymbols: " + ", ".join(symbols) + "\n\n" + reads
    )


def _get_json(url: str, user: str | None, password: str | None) -> Any:
    import base64
    import urllib.request
    req = urllib.request.Request(url)
    if user and password:
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        req.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (operator-run, trusted host)
        return json.load(resp)


def _main(argv: list[str]) -> int:
    import os
    p = argparse.ArgumentParser(description="Reconcile TradingView chart values vs the bot's feed.")
    p.add_argument("--api-base", required=True, help="e.g. https://your-host.example.com")
    p.add_argument("--symbols", required=True, help="comma-separated, e.g. F,SOFI,BULL")
    p.add_argument("--chart-json", help="path to chart-values JSON ('-' for stdin); omit to print recipe")
    p.add_argument("--max-age", type=int, help="override staleness threshold (else read live /api/config)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = p.parse_args(argv)

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not args.chart_json:
        print(_recipe(symbols))
        return 0

    raw = sys.stdin.read() if args.chart_json == "-" else open(args.chart_json).read()
    chart_by_symbol = json.loads(raw)

    user, password = os.environ.get("DASHBOARD_USER"), os.environ.get("DASHBOARD_PASSWORD")
    base = args.api_base.rstrip("/")
    indicators = _get_json(f"{base}/api/tv-indicators", user, password).get("indicators", [])
    max_age = args.max_age
    if max_age is None:
        try:
            cfg = _get_json(f"{base}/api/config", user, password)
            max_age = int(cfg["editable"]["ai"]["tv_indicator_max_age_seconds"])
        except Exception:  # noqa: BLE001 — fall back to the repo default if config read fails
            max_age = 108_000

    reports = reconcile(chart_by_symbol, indicators, tol=ReconcileTolerance(), max_age_seconds=max_age)
    if args.json:
        print(json.dumps([{
            "symbol": r.symbol, "ok": r.ok, "stale": r.stale, "bot_age_seconds": r.bot_age_seconds,
            "divergences": [d.__dict__ for d in r.divergences],
        } for r in reports], indent=2))
    else:
        print(format_report(reports))
    return 0 if all(r.ok for r in reports) else 1


def main() -> None:
    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
