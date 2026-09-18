"""Replay the technical-setup detectors over historical daily bars and report which patterns paid.

Read-only calibration: pulls ``--days`` of daily bars per watchlist name from the configured Alpaca
market data (Robinhood bars carry no dates, so the replay needs Alpaca keys), walks forward bar by
bar exactly as the scanner would, and aggregates the realized 5/10-day forward returns and max
adverse excursion per setup label. Answers "which setups have edge on MY names" today, instead of
waiting weeks for live fires.

    python -m agentic.tools.setup_replay --days 730 [--symbols SMR,BULL] [--json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _run(days: int, symbols: list[str] | None, as_json: bool) -> int:
    from ..config import load_config
    from ..marketdata.alpaca_md import AlpacaMarketData
    from ..services.setup_tracker import aggregate_accuracy, replay_history

    s = load_config()
    md = AlpacaMarketData(feed=s.entry.feed)
    if not getattr(md, "_available", False):
        print("Alpaca market data unavailable (need ALPACA_API_KEY/SECRET) -- replay needs dated bars.",
              file=sys.stderr)
        return 2
    names = [x.upper() for x in (symbols or s.entry.watchlist)]
    bars_by: dict[str, list[dict]] = {}
    for sym in names:
        try:
            bars_by[sym] = await md.get_underlying_bars(sym, days)
        except Exception as exc:  # noqa: BLE001 -- one bad name must not sink the replay
            print(f"{sym}: bars fetch failed: {exc}", file=sys.stderr)
    rows = replay_history(bars_by, s.entry.setups)
    acc = aggregate_accuracy(rows)
    per_symbol = {sym: sum(1 for r in rows if r["symbol"] == sym) for sym in bars_by}
    if as_json:
        print(json.dumps({"days": days, "symbols": names, "fires": len(rows),
                          "per_symbol": per_symbol, "accuracy": acc}, indent=2))
        return 0
    print(f"Replay: {len(rows)} fires across {len(bars_by)} names over ~{days} calendar days "
          f"(bars/name: {', '.join(f'{k}={len(v)}' for k, v in bars_by.items())})")
    pct = lambda v: f"{v * 100:+.1f}%" if v is not None else "  n/a"  # noqa: E731
    hrf = lambda v: f"{v * 100:.0f}%" if v is not None else "n/a"     # noqa: E731
    print(f"{'label':26}{'bias':10}{'n':>6}{'n_ep':>6} | all: {'hit5d':>5}{'avg10d':>8}{'mae10d':>8}"
          f" | episodes: {'hit5d':>5}{'avg10d':>8}{'mae10d':>8}")
    for a in acc:
        print(f"{a['label']:26}{a['bias']:10}{a['n']:>6}{a['n_ep']:>6} | "
              f"     {hrf(a['hit_rate_5d']):>5}{pct(a['avg_ret_10d']):>8}{pct(a['avg_mae_10d']):>8}"
              f" |           {hrf(a['ep_hit_rate_5d']):>5}{pct(a['ep_avg_ret_10d']):>8}{pct(a['ep_avg_mae_10d']):>8}")
    print("hit5d: FAVORABLE = didn't fall over 5 bars; AVOID = did fall. mae10d = worst 10-bar "
          "excursion vs fire price (the put-seller's question). n_ep = episode starts (a setup that "
          "persists re-fires each bar with overlapping windows) -- the honest count. Small n = weak.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument("--days", type=int, default=730)
    p.add_argument("--symbols", default=None, help="comma-separated; default = watchlist")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    syms = [x.strip() for x in a.symbols.split(",") if x.strip()] if a.symbols else None
    return asyncio.run(_run(a.days, syms, a.json))


if __name__ == "__main__":
    raise SystemExit(main())
