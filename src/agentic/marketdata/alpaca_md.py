"""Alpaca options market data provider (stub wired to the snapshots endpoint).

Uses the REST options-snapshots endpoint:
    GET https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}
which returns latest quote (bid/ask), latest trade, Greeks and IV per contract.

Free tier exposes the delayed "indicative" feed; real-time OPRA requires the Algo
Trader Plus subscription. ``feed`` is configurable accordingly.

This is implemented with httpx so it has no hard dependency on alpaca-py; swap to the
SDK later if preferred. Network/credential errors return None so the monitor degrades
gracefully (the broker-side quote is the fallback).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from ..domain.models import utcnow

from ..config import get_secret
from ..domain.models import Position
from .base import MarketDataProvider
from .quote import OptionContractQuote, OptionQuote

log = logging.getLogger("agentic.marketdata.alpaca")

_BASE = "https://data.alpaca.markets/v1beta1/options/snapshots"
_MOST_ACTIVES = "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives"
_STOCK_SNAPSHOTS = "https://data.alpaca.markets/v2/stocks/snapshots"
_STOCK_SNAPSHOT = "https://data.alpaca.markets/v2/stocks/{symbol}/snapshot"
_STOCK_BARS = "https://data.alpaca.markets/v2/stocks/{symbol}/bars"
_OPTION_BARS = "https://data.alpaca.markets/v1beta1/options/bars"
_OPTION_CONTRACTS = "https://api.alpaca.markets/v2/options/contracts"


def parse_occ_symbol(occ: str) -> tuple[str, date, str, float] | None:
    """Parse an OCC option symbol -> (underlying, expiration, type, strike).

    Layout: <root><YYMMDD><C|P><strike*1000, 8 digits>. The trailing 15 chars are fixed,
    so the root is everything before them. Returns None if it doesn't parse.
    """
    if len(occ) < 16:
        return None
    try:
        tail = occ[-15:]
        root = occ[:-15]
        yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
        cp = tail[6].upper()
        strike = int(tail[7:15]) / 1000.0
        if cp not in ("C", "P") or not root:
            return None
        return root, date(2000 + yy, mm, dd), "call" if cp == "C" else "put", strike
    except (ValueError, IndexError):
        return None


class AlpacaMarketData(MarketDataProvider):
    def __init__(self, feed: str = "indicative"):
        self.feed = feed
        self._key = get_secret("ALPACA_API_KEY")
        self._secret = get_secret("ALPACA_API_SECRET")
        try:
            import httpx  # noqa: F401
            self._available = True
        except ImportError:
            log.warning("httpx not installed; AlpacaMarketData disabled. Install the 'web' extra.")
            self._available = False

    @property
    def is_realtime(self) -> bool:
        """True only on the OPRA feed; the indicative feed is delayed/modified."""
        return self.feed == "opra"

    def _headers(self) -> dict[str, str]:
        return {"APCA-API-KEY-ID": self._key or "", "APCA-API-SECRET-KEY": self._secret or ""}

    async def most_active_symbols(self, top: int = 100) -> list[str]:
        """Most-active US equities by volume — a liquid discovery universe for the opportunity
        scan. Empty list on any failure (the scan degrades to whatever universe it has)."""
        if not self._available or not self._key or not self._secret:
            return []
        import httpx
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Alpaca caps `top` at 100; a larger value 400s (and returned an empty universe).
                resp = await client.get(_MOST_ACTIVES,
                                        params={"by": "volume", "top": min(int(top), 100)},
                                        headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            log.warning("Alpaca most-actives failed: %s", exc)
            return []
        return [r["symbol"] for r in (data.get("most_actives") or []) if r.get("symbol")]

    async def snapshot_prices(self, symbols: list[str]) -> dict[str, float]:
        """Latest price per symbol (batch stock snapshots), for price-band filtering. Uses the IEX
        feed so it works on any data tier; unknown symbols are simply absent from the result."""
        if not symbols or not self._available or not self._key or not self._secret:
            return {}
        import httpx
        out: dict[str, float] = {}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for i in range(0, len(symbols), 100):
                    chunk = symbols[i:i + 100]
                    resp = await client.get(
                        _STOCK_SNAPSHOTS,
                        params={"symbols": ",".join(chunk), "feed": "iex"},
                        headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    snaps = data.get("snapshots") or data
                    for sym, snap in (snaps or {}).items():
                        if not isinstance(snap, dict):
                            continue
                        p = ((snap.get("latestTrade") or {}).get("p")
                             or (snap.get("dailyBar") or {}).get("c")
                             or (snap.get("prevDailyBar") or {}).get("c"))
                        if p:
                            out[sym.upper()] = float(p)
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning("Alpaca snapshot prices failed: %s", exc)
        return out

    async def get_quote(self, position: Position) -> OptionQuote | None:
        if not self._available or not self._key or not self._secret:
            return None
        import httpx

        url = f"{_BASE}/{position.underlying}"
        params = {"feed": self.feed, "limit": 1000}
        headers = {"APCA-API-KEY-ID": self._key, "APCA-API-SECRET-KEY": self._secret}
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, params=params, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001 — degrade gracefully on any data error
            log.warning("Alpaca snapshot failed for %s: %s", position.occ_symbol, exc)
            return None

        snap = (data.get("snapshots") or {}).get(position.occ_symbol)
        if not snap:
            return None
        quote = snap.get("latestQuote") or {}
        greeks = snap.get("greeks") or {}
        bid = quote.get("bp")
        ask = quote.get("ap")
        mark = round((bid + ask) / 2, 4) if bid and ask else None
        return OptionQuote(
            occ_symbol=position.occ_symbol,
            bid=bid,
            ask=ask,
            mark=mark,
            delta=greeks.get("delta"),
            iv=snap.get("impliedVolatility"),
        )

    async def get_chain(self, underlying: str) -> list[OptionContractQuote]:
        """Fetch the full option chain for an underlying via the snapshots endpoint.

        Same endpoint as get_quote but we consume every contract (paginated). Open interest
        is not always present on Alpaca snapshots; left as None when absent (the screener
        treats unknown OI as 'cannot verify' rather than failing the liquidity floor).
        """
        if not self._available or not self._key or not self._secret:
            return []
        import httpx

        url = f"{_BASE}/{underlying.upper()}"
        out: list[OptionContractQuote] = []
        page_token: str | None = None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for _ in range(20):  # hard page cap (safety)
                    params: dict = {"feed": self.feed, "limit": 1000}
                    if page_token:
                        params["page_token"] = page_token
                    resp = await client.get(url, params=params, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    snapshots = data.get("snapshots") or {}
                    for occ, snap in snapshots.items():
                        parsed = parse_occ_symbol(occ)
                        if parsed is None:
                            continue
                        root, exp, opt_type, strike = parsed
                        quote = snap.get("latestQuote") or {}
                        greeks = snap.get("greeks") or {}
                        daily = snap.get("dailyBar") or {}
                        bid = quote.get("bp")
                        ask = quote.get("ap")
                        mark = round((bid + ask) / 2, 4) if bid and ask else None
                        out.append(OptionContractQuote(
                            occ_symbol=occ,
                            underlying=root,
                            option_id=None,  # resolved to a broker UUID at order time
                            option_type=opt_type,
                            strike=strike,
                            expiration=exp,
                            bid=bid,
                            ask=ask,
                            mark=mark,
                            delta=greeks.get("delta"),
                            theta=greeks.get("theta"),
                            gamma=greeks.get("gamma"),
                            vega=greeks.get("vega"),
                            iv=snap.get("impliedVolatility"),
                            open_interest=snap.get("openInterest"),
                            volume=daily.get("v"),
                        ))
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
        except Exception as exc:  # noqa: BLE001 — degrade gracefully on any data error
            log.warning("Alpaca chain fetch failed for %s: %s", underlying, exc)
            return out
        return out

    async def get_underlying_price(self, underlying: str) -> float | None:
        if not self._available or not self._key or not self._secret:
            return None
        import httpx

        url = _STOCK_SNAPSHOT.format(symbol=underlying.upper())
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            log.warning("Alpaca underlying price failed for %s: %s", underlying, exc)
            return None
        trade = data.get("latestTrade") or {}
        daily = data.get("dailyBar") or {}
        return trade.get("p") or daily.get("c")

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260) -> list[dict]:
        """Daily OHLCV bars (oldest->newest) for technicals, via the Alpaca stock bars endpoint."""
        if not self._available or not self._key or not self._secret:
            return []
        import httpx

        start = (utcnow().date() - timedelta(days=lookback_days)).isoformat()
        url = _STOCK_BARS.format(symbol=underlying.upper())
        params = {"timeframe": "1Day", "start": start, "limit": 10000, "adjustment": "raw"}
        out: list[dict] = []
        page_token: str | None = None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for _ in range(10):
                    p = dict(params)
                    if page_token:
                        p["page_token"] = page_token
                    resp = await client.get(url, params=p, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    for b in data.get("bars") or []:
                        t = b.get("t")
                        out.append({"date": t[:10] if t else None, "o": b.get("o"),
                                    "h": b.get("h"), "l": b.get("l"), "c": b.get("c"),
                                    "v": b.get("v")})
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
        except Exception as exc:  # noqa: BLE001 — degrade gracefully
            log.warning("Alpaca bars fetch failed for %s: %s", underlying, exc)
            return out
        return out

    async def list_option_contracts(
        self, underlying: str, exp_gte: str, exp_lte: str, opt_type: str = "put"
    ) -> list[dict]:
        """List real (incl. expired) option contracts for the historical IV backfill.

        Returns [{symbol, strike, expiration, type}]. Uses the Alpaca Trading API contracts
        endpoint so we never guess OCC symbols. TODO(verify-live): confirm field names.
        """
        if not self._available or not self._key or not self._secret:
            return []
        import httpx

        out: list[dict] = []
        page_token: str | None = None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                for _ in range(50):
                    params: dict = {
                        "underlying_symbols": underlying.upper(), "type": opt_type,
                        "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte,
                        "limit": 10000,
                    }
                    if page_token:
                        params["page_token"] = page_token
                    resp = await client.get(_OPTION_CONTRACTS, params=params, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    for c in data.get("option_contracts") or []:
                        try:
                            out.append({
                                "symbol": c.get("symbol"),
                                "strike": float(c.get("strike_price")),
                                "expiration": c.get("expiration_date"),
                                "type": c.get("type"),
                            })
                        except (TypeError, ValueError):
                            continue
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("Alpaca contracts list failed for %s: %s", underlying, exc)
            return out
        return out

    async def get_option_bars(self, symbol: str, start: str, end: str) -> list[dict]:
        """Daily bars for one option contract over [start, end] (ISO dates). [{date, c}...]."""
        if not self._available or not self._key or not self._secret:
            return []
        import httpx

        out: list[dict] = []
        page_token: str | None = None
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                for _ in range(20):
                    params: dict = {"symbols": symbol, "timeframe": "1Day",
                                    "start": start, "end": end, "limit": 10000}
                    if page_token:
                        params["page_token"] = page_token
                    resp = await client.get(_OPTION_BARS, params=params, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    for b in (data.get("bars") or {}).get(symbol) or []:
                        t = b.get("t")
                        out.append({"date": t[:10] if t else None, "c": b.get("c")})
                    page_token = data.get("next_page_token")
                    if not page_token:
                        break
        except Exception as exc:  # noqa: BLE001
            log.warning("Alpaca option bars failed for %s: %s", symbol, exc)
            return out
        return out
