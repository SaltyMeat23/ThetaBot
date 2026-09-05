"""On-demand CSP screener over an arbitrary universe — the interactive counterpart to the
auto-scanner. Fetches each symbol's option chain and applies the same screen_candidates logic
(criteria + liquidity hygiene), ranked by theta-efficiency. Read-only; never sizes or trades.
"""
from __future__ import annotations

import logging

from ..config import EntryCriteria
from ..entry.screener import EntryCandidate, screen_candidates
from ..marketdata.base import MarketDataProvider

log = logging.getLogger("agentic.screening")

# Leveraged / inverse ETFs dominate the most-active-by-volume list but are NOT wheel candidates —
# they decay and aren't "ownable" like a business. Excluded from discovery by default so the surfaced
# ideas are closer to real underlyings. NOT exhaustive; the human still applies the quality judgment.
_LEVERAGED_INVERSE_ETFS = frozenset({
    "TQQQ", "SQQQ", "SPXL", "SPXU", "SPXS", "UPRO", "SDOW", "UDOW", "TNA", "TZA", "SOXL", "SOXS",
    "TSLL", "TSLQ", "TSLS", "TSLZ", "NVDL", "NVDU", "NVDD", "NVDS", "MSTU", "MSTX", "MSTZ",
    "AMDL", "AMDU", "CONL", "APLD", "FAS", "FAZ", "LABU", "LABD", "YINN", "YANG", "JNUG", "JDST",
    "NUGT", "DUST", "GUSH", "DRIP", "BOIL", "KOLD", "UCO", "SCO", "UVXY", "SVXY", "VXX", "VIXY",
    "BULZ", "WEBL", "WEBS", "FNGU", "FNGD", "BITU", "SBIT", "ETHU", "AGQ", "ZSL", "TMF", "TMV",
    "URTY", "SRTY", "BITX", "MSTY", "PLTU", "HIMZ", "SMCX", "SMCL",
})


async def screen_universe(
    market_data: MarketDataProvider,
    symbols: list[str],
    criteria: EntryCriteria,
    *,
    limit: int = 50,
) -> list[EntryCandidate]:
    """Screen each symbol's chain for CSP candidates and return the top ``limit`` by
    theta-efficiency. A symbol whose chain can't be fetched is skipped, not fatal."""
    out: list[EntryCandidate] = []
    for sym in symbols:
        try:
            chain = await market_data.get_chain(sym)
        except Exception as exc:  # noqa: BLE001 — one bad symbol must not sink the screen
            log.warning("screener: chain fetch failed for %s: %s", sym, exc)
            continue
        out.extend(screen_candidates(sym, chain, criteria))
    out.sort(key=lambda c: c.theta_efficiency, reverse=True)
    return out[:limit]


async def opportunity_scan(
    market_data,
    criteria: EntryCriteria,
    *,
    price_min: float,
    price_max: float,
    universe_size: int = 100,
    max_screen: int = 25,
    limit: int = 60,
    exclude: frozenset[str] | set[str] | None = None,
) -> dict:
    """Discover CSP opportunities across Alpaca's most-active universe within a price band.

    Pulls the most-active symbols, filters to ``price_min <= last <= price_max``, caps the number
    of chains fetched (``max_screen``, latency guard — most-active order keeps the busiest names),
    screens each, and ranks by theta-efficiency. Returns metadata + candidates (with underlying
    price attached). Requires a provider exposing most_active_symbols()/snapshot_prices() — returns
    an empty result otherwise (e.g. paper).
    """
    if not (hasattr(market_data, "most_active_symbols") and hasattr(market_data, "snapshot_prices")):
        return {"universe": 0, "scanned": [], "prices": {}, "candidates": []}
    skip = (_LEVERAGED_INVERSE_ETFS | set(exclude)) if exclude else _LEVERAGED_INVERSE_ETFS
    syms = await market_data.most_active_symbols(universe_size)
    prices = await market_data.snapshot_prices(syms) if syms else {}
    in_band = [s for s in syms
               if price_min <= prices.get(s, -1) <= price_max and s not in skip][:max_screen]
    cands: list[EntryCandidate] = []
    for sym in in_band:
        try:
            chain = await market_data.get_chain(sym)
        except Exception as exc:  # noqa: BLE001
            log.warning("opportunity scan: chain fetch failed for %s: %s", sym, exc)
            continue
        cands.extend(screen_candidates(sym, chain, criteria))
    cands.sort(key=lambda c: c.theta_efficiency, reverse=True)
    return {"universe": len(syms), "scanned": in_band,
            "prices": {s: prices[s] for s in in_band if s in prices}, "candidates": cands[:limit]}
