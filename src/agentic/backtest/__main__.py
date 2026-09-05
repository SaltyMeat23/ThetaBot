"""CLI: python -m agentic.backtest F SOFI --from 2024-06-01 --to 2026-06-01

Runs the CSP backtest per symbol against Alpaca history, prints a report, and writes JSON.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
from datetime import date

from ..config import load_config
from ..logging_setup import setup_logging
from ..marketdata.alpaca_md import AlpacaMarketData
from .engine import LIMITATIONS, CspBacktest


def _print_report(r) -> None:
    print(f"\n=== Backtest {r.symbol}  {r.start} -> {r.end} ===")
    if not r.n_trades:
        print("  no trades (no contracts/bars resolved, or nothing passed the screen)")
        return
    wr = f"{r.win_rate * 100:.0f}%" if r.win_rate is not None else "—"
    ar = f"{r.annualized_return * 100:.1f}%" if r.annualized_return is not None else "—"
    asr = f"{r.assignment_rate * 100:.0f}%" if r.assignment_rate is not None else "—"
    print(f"  trades={r.n_trades}  win_rate={wr}  total_pnl=${r.total_pnl:,.2f}  "
          f"avg=${r.avg_pnl:,.2f}")
    print(f"  max_drawdown=${r.max_drawdown:,.2f}  assignment_rate={asr}  "
          f"avg_dte_held={r.avg_dte_held}  annualized(on collateral)={ar}")


async def _main(symbols: list[str], start: date, end: date) -> None:
    settings = load_config()
    md = AlpacaMarketData(feed=settings.entry.feed)
    bt = CspBacktest(md, settings.entry.criteria)
    print("!!! " + LIMITATIONS)
    for sym in symbols:
        r = await bt.run(sym, start, end)
        _print_report(r)
        path = f"backtest_{sym}_{start}_{end}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(dataclasses.asdict(r), f, indent=2)
        print(f"  -> {path}")


def main() -> None:
    setup_logging()
    p = argparse.ArgumentParser(prog="agentic.backtest")
    p.add_argument("symbols", nargs="+")
    p.add_argument("--from", dest="start", required=True, help="YYYY-MM-DD")
    p.add_argument("--to", dest="end", required=True, help="YYYY-MM-DD")
    a = p.parse_args()
    asyncio.run(_main([s.upper() for s in a.symbols],
                      date.fromisoformat(a.start), date.fromisoformat(a.end)))


if __name__ == "__main__":
    main()
