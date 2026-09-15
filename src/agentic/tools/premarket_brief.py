"""Driver for the on-demand pre-market watchlist brief. Read-only.

Fetches the bot's feed/health/config over HTTP, takes the chart values I read via the TradingView
MCP (a JSON file: {"F": {"price":9.56,"support":9.55,"resistance":10.19,"adx":22.5,
"bb_percent_b":48.1,"sma200":8.9}, ...}), and writes a markdown brief.

Run:  python -m agentic.tools.premarket_brief --api-base https://host --chart-json chart.json \
          [--symbols F,SOFI] [--out brief.md]
      (omit --chart-json to print the MCP-read recipe first)
"""
from __future__ import annotations

import argparse
import json
import sys

from ..services.premarket_brief import build_premarket_brief
from .tv_reconcile import _get_json


def _recipe(symbols: list[str] | None) -> str:
    return (
        "For each watchlist symbol, read via the TradingView MCP then pass as --chart-json:\n"
        "  price (quote_get), support=lowest low(20) & resistance=highest high(20) (data_get_ohlcv),\n"
        "  adx & bb_percent_b (data_get_study_values), sma200 (data_get_study_values or bars).\n"
        "JSON shape: {\"F\": {\"price\": 9.56, \"support\": 9.55, \"resistance\": 10.19,\n"
        "  \"adx\": 22.5, \"bb_percent_b\": 48.1, \"sma200\": 8.9}, ...}\n"
        + (f"Symbols: {', '.join(symbols)}\n" if symbols else "")
    )


def _main(argv: list[str]) -> int:
    import os
    p = argparse.ArgumentParser(description="Build an on-demand pre-market watchlist brief.")
    p.add_argument("--api-base", required=True)
    p.add_argument("--symbols", help="comma-separated; defaults to the bot's live watchlist")
    p.add_argument("--chart-json", help="path to chart-values JSON ('-' for stdin); omit for recipe")
    p.add_argument("--watchlist-name", default="watchlist")
    p.add_argument("--out", help="write markdown here; default stdout")
    args = p.parse_args(argv)

    explicit = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else None
    if not args.chart_json:
        print(_recipe(explicit))
        return 0

    user, password = os.environ.get("DASHBOARD_USER"), os.environ.get("DASHBOARD_PASSWORD")
    base = args.api_base.rstrip("/")
    indicators = _get_json(f"{base}/api/tv-indicators", user, password).get("indicators", [])
    tv_health = _get_json(f"{base}/api/tv-health", user, password)
    bot_config = _get_json(f"{base}/api/config", user, password)
    symbols = explicit or ((bot_config.get("editable") or {}).get("entry") or {}).get("watchlist") or []

    raw = sys.stdin.read() if args.chart_json == "-" else open(args.chart_json).read()
    chart_by_symbol = json.loads(raw)

    _title, body = build_premarket_brief(
        watchlist_name=args.watchlist_name, symbols=symbols, chart_by_symbol=chart_by_symbol,
        bot_indicators=indicators, tv_health=tv_health, bot_config=bot_config,
    )
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"Wrote {args.out}")
    else:
        print(body)
    return 0


def main() -> None:
    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
