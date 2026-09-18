"""Strike-survival report for the watchlist: how often a put k cushions below spot gets touched /
finishes in the money over H bars, realized vs the theoretical (delta-implied) probability, and
conditioned on the technical setup active at entry.

    python -m agentic.tools.strike_survival --days 730 [--symbols SMR,BULL] [--unit em|atr|pct]
                                             [--cushion 0.7 --horizon 10] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from ..entry.setups import label_bias


def _pct(v) -> str:
    return f"{v * 100:5.1f}%" if v is not None else "  n/a"


async def _run(days: int, symbols: list[str] | None, unit: str, cushion: float, horizon: int,
               as_json: bool) -> int:
    from ..config import load_config
    from ..marketdata.alpaca_md import AlpacaMarketData
    from ..services.strike_survival import labels_by_index, strike_survival

    s = load_config()
    md = AlpacaMarketData(feed=s.entry.feed)
    if not getattr(md, "_available", False):
        print("Alpaca market data unavailable (need ALPACA_API_KEY/SECRET).", file=sys.stderr)
        return 2
    names = [x.upper() for x in (symbols or s.entry.watchlist)]
    bars_by: dict[str, list[dict]] = {}
    for sym in names:
        try:
            bars_by[sym] = await md.get_underlying_bars(sym, days)
        except Exception as exc:  # noqa: BLE001
            print(f"{sym}: bars fetch failed: {exc}", file=sys.stderr)
    labels_by = {sym: labels_by_index(b, s.entry.setups) for sym, b in bars_by.items()}
    rows = strike_survival(bars_by, unit=unit, labels_by_symbol=labels_by)
    if as_json:
        print(json.dumps({"days": days, "unit": unit, "rows": rows}, indent=2))
        return 0

    pooled = [r for r in rows if r["symbol"] == "ALL" and r["label"] == "ALL"]
    print(f"Strike survival, pooled over {len(bars_by)} names, ~{days} days, cushion unit = {unit}")
    print(f"{'cushion':>8}{'H':>4}{'n':>7}{'touched':>9}{'ITM@exp':>9}{'theory ITM':>12}{'theory touch':>14}{'avg worst':>11}")
    for r in pooled:
        print(f"{r['cushion']:>8}{r['horizon']:>4}{r['n']:>7}{_pct(r['touch_rate']):>9}{_pct(r['itm_rate']):>9}"
              f"{_pct(r['p_itm_theory']):>12}{_pct(r['p_touch_theory']):>14}{_pct(r['avg_worst_pct']):>11}")

    print(f"\nBy setup active at entry (cushion {cushion} {unit}, H={horizon}); n >= 15 shown:")
    print(f"{'label':26}{'bias':10}{'n':>7}{'touched':>9}{'ITM@exp':>9}{'avg worst':>11}")
    cond = [r for r in rows if r["symbol"] == "ALL" and r["cushion"] == cushion
            and r["horizon"] == horizon and r["n"] >= 15]
    for r in sorted(cond, key=lambda x: x["touch_rate"]):
        bias = label_bias(r["label"]) if r["label"] != "ALL" else "-"
        print(f"{r['label']:26}{bias:10}{r['n']:>7}{_pct(r['touch_rate']):>9}{_pct(r['itm_rate']):>9}{_pct(r['avg_worst_pct']):>11}")

    print(f"\nPer name (cushion {cushion} {unit}, H={horizon}, all bars):")
    print(f"{'symbol':8}{'n':>7}{'touched':>9}{'ITM@exp':>9}{'avg worst':>11}")
    per = [r for r in rows if r["symbol"] != "ALL" and r["label"] == "ALL"
           and r["cushion"] == cushion and r["horizon"] == horizon]
    for r in sorted(per, key=lambda x: x["touch_rate"]):
        print(f"{r['symbol']:8}{r['n']:>7}{_pct(r['touch_rate']):>9}{_pct(r['itm_rate']):>9}{_pct(r['avg_worst_pct']):>11}")
    print("\ntouched = low <= strike at any point in the window (roll/assignment pressure); ITM@exp = close "
          "below strike on the last bar. Theory = zero-drift lognormal at the realized-vol cushion "
          "(N(-k) ~ what a delta of that size implies). Realized > theory means these names run "
          "wilder than the model assumes.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--symbols", default=None)
    p.add_argument("--unit", default="em", choices=("em", "atr", "pct"))
    p.add_argument("--cushion", type=float, default=0.7)
    p.add_argument("--horizon", type=int, default=10)
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    syms = [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    return asyncio.run(_run(a.days, syms, a.unit, a.cushion, a.horizon, a.json))


if __name__ == "__main__":
    raise SystemExit(main())
