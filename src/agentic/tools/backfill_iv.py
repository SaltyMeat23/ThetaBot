"""Historical ATM implied-volatility backfill → seeds iv_history so IV Rank works now.

Reconstructs a monthly ATM IV series (~2yr) from Alpaca historical option *prices* (no greeks
available) via Black-Scholes inversion. Per monthly put expiration: pick the ATM strike at
~35 DTE, fetch that real contract's bar, and solve IV. Best-effort + idempotent (record_iv
upserts) — coverage is sparse where contracts/bars are missing, and it's a benign ~2yr window,
so treat resulting IV Rank as a regime signal, not gospel.

Run:  python -m agentic.tools.backfill_iv F SOFI
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import date, timedelta

from ..domain.models import utcnow
from ..marketdata.blackscholes import DEFAULT_RATE, implied_vol

log = logging.getLogger("agentic.tools.backfill_iv")

TARGET_DTE = 35   # ATM contract chosen ~35 days before its expiration


def _nearest_prior_date(sorted_dates: list[str], target: date) -> str | None:
    """Latest available date on/before target (the trading day we'll price from)."""
    chosen = None
    tgt = target.isoformat()
    for d in sorted_dates:
        if d <= tgt:
            chosen = d
        else:
            break
    return chosen


class IvBackfill:
    def __init__(self, market_data, journal, rate: float = DEFAULT_RATE):
        self.market_data = market_data
        self.journal = journal
        self.rate = rate

    async def backfill_symbol(self, symbol: str, lookback_days: int = 730) -> int:
        """Backfill one symbol's iv_history. Returns the number of IV points recorded."""
        bars = await self.market_data.get_underlying_bars(symbol, lookback_days)
        closes = {b["date"]: b["c"] for b in bars if b.get("date") and b.get("c")}
        if not closes:
            return 0
        dates_sorted = sorted(closes)
        today = utcnow().date()
        contracts = await self.market_data.list_option_contracts(
            symbol,
            exp_gte=(today - timedelta(days=lookback_days)).isoformat(),
            exp_lte=(today + timedelta(days=60)).isoformat(),
            opt_type="put",
        )
        by_exp: dict[str, list[dict]] = defaultdict(list)
        for c in contracts:
            if c.get("expiration") and c.get("symbol") and c.get("strike"):
                by_exp[c["expiration"]].append(c)

        recorded = 0
        for exp_str, cs in by_exp.items():
            try:
                exp = date.fromisoformat(exp_str)
            except ValueError:
                continue
            entry_str = _nearest_prior_date(dates_sorted, exp - timedelta(days=TARGET_DTE))
            if entry_str is None:
                continue
            entry_dt = date.fromisoformat(entry_str)
            dte = (exp - entry_dt).days
            if dte <= 5:
                continue
            spot = closes[entry_str]
            atm = min(cs, key=lambda c: abs(c["strike"] - spot))
            obars = await self.market_data.get_option_bars(
                atm["symbol"], (entry_dt - timedelta(days=3)).isoformat(), entry_str
            )
            price = None
            for b in obars:  # latest bar on/before the entry date
                if b.get("date") and b["date"] <= entry_str and b.get("c"):
                    price = b["c"]
            if price is None:
                continue
            iv = implied_vol(price, spot, atm["strike"], dte / 365.0, self.rate, is_call=False)
            if iv is None:
                continue
            self.journal.record_iv(symbol, entry_dt, iv)
            recorded += 1
        log.info("IV backfill %s: recorded %d point(s).", symbol, recorded)
        return recorded

    async def run(self, symbols: list[str], lookback_days: int = 730) -> dict[str, int]:
        return {s: await self.backfill_symbol(s, lookback_days) for s in symbols}


async def _main(symbols: list[str]) -> None:
    from ..config import load_config
    from ..marketdata.alpaca_md import AlpacaMarketData
    from ..store.db import Database
    from ..store.trade_journal import TradeJournalStore

    settings = load_config()
    md = AlpacaMarketData(feed=settings.entry.feed)
    journal = TradeJournalStore(Database(settings.db_path))
    counts = await IvBackfill(md, journal).run(symbols)
    print("IV backfill complete:", counts)


def main() -> None:
    import sys
    from ..logging_setup import setup_logging

    setup_logging()
    symbols = [s.upper() for s in sys.argv[1:]]
    if not symbols:
        print("usage: python -m agentic.tools.backfill_iv SYMBOL [SYMBOL ...]")
        return
    asyncio.run(_main(symbols))


if __name__ == "__main__":
    main()
