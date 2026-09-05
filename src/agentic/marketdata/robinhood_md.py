"""Robinhood-backed market-data provider.

Sources ALL option + equity data from the *same* Robinhood MCP connection used for trading, so an
operator needs only ONE login — no separate Alpaca (or paid OPRA) subscription, no TradingView.

Built for a slow (7-14 DTE) wheel: narrow queries keep RH call volume modest — only near-DTE
expirations, out-of-the-money strikes, batched quotes — with a short per-underlying cache and
exponential backoff on transient (429 / 502) errors. A full one-name CSP screen is ~7 RH calls.

It reuses the RobinhoodMCPBroker's ``_call_tool`` / ``_iter_records`` (and thus its single OAuth
session), so option and equity data ride the same authenticated, serialized channel as trading.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from ..domain.models import Position
from .base import MarketDataProvider
from .quote import OptionContractQuote, OptionQuote

log = logging.getLogger("agentic.md.robinhood")

_OCC = re.compile(r"^([A-Z.]+)(\d{6})([CP])(\d{8})$")


def _f(x: object) -> float | None:
    try:
        return float(x) if x is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _i(x: object) -> int | None:
    try:
        return int(float(x)) if x is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_occ(sym: str):
    m = _OCC.match(sym)
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    return (root, date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:6])),
            "call" if cp == "C" else "put", int(strike) / 1000.0)


def _occ(underlying: str, exp: date, typ: str, strike: float) -> str:
    return f"{underlying}{exp:%y%m%d}{'C' if typ == 'call' else 'P'}{int(round(strike * 1000)):08d}"


class RobinhoodMarketData(MarketDataProvider):
    """Implements the full MarketDataProvider contract against Robinhood's MCP tools."""

    def __init__(self, broker, *, dte_window_days: int = 21,
                 chain_ttl_seconds: float = 240, bars_ttl_seconds: float = 3600):
        self._b = broker
        self._dte_window = dte_window_days
        self._chain_ttl = chain_ttl_seconds
        self._bars_ttl = bars_ttl_seconds
        self._chain_cache: dict[str, tuple[float, list[OptionContractQuote]]] = {}
        self._bars_cache: dict[str, tuple[float, list[dict]]] = {}

    async def _call(self, tool: str, args: dict, *, retries: int = 2):
        """One RH tool call with exponential backoff on transient errors."""
        delay = 0.6
        for attempt in range(retries + 1):
            try:
                return await self._b._call_tool(tool, args)
            except Exception as exc:  # noqa: BLE001 — market data is best-effort; never crash a scan
                msg = str(exc).lower()
                transient = any(s in msg for s in ("429", "502", "503", "timeout", "reset", "temporarily"))
                if attempt < retries and transient:
                    await asyncio.sleep(delay)
                    delay *= 2
                    continue
                log.warning("RH market-data call %s failed: %s", tool, exc)
                raise

    # --- underlying ------------------------------------------------------------------------------
    async def get_underlying_price(self, underlying: str) -> float | None:
        try:
            q = await self._call("get_equity_quotes", {"symbols": [underlying]})
        except Exception:  # noqa: BLE001
            return None
        recs = self._b._iter_records(q)
        if not recs:
            return None
        inner = recs[0].get("quote") if isinstance(recs[0].get("quote"), dict) else recs[0]
        for k in ("last_trade_price", "last_non_reg_trade_price", "previous_close"):
            v = _f(inner.get(k))
            if v:
                return v
        bid, ask = _f(inner.get("bid_price")), _f(inner.get("ask_price"))
        return round((bid + ask) / 2, 4) if (bid and ask) else None

    async def get_underlying_bars(self, underlying: str, lookback_days: int = 260) -> list[dict]:
        c = self._bars_cache.get(underlying)
        if c and time.time() - c[0] < self._bars_ttl:
            return c[1]
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback_days + 5)
        try:
            h = await self._call("get_equity_historicals", {
                "symbols": [underlying], "interval": "day",
                "start_time": start.strftime("%Y-%m-%dT00:00:00Z"),
                "end_time": end.strftime("%Y-%m-%dT00:00:00Z")})
        except Exception:  # noqa: BLE001
            return self._bars_cache.get(underlying, (0, []))[1]
        recs = self._b._iter_records(h)
        raw = (recs[0].get("bars") if recs else None) or []
        bars: list[dict] = []
        for bar in raw:
            if bar.get("session") not in (None, "reg"):
                continue
            cl = _f(bar.get("close_price"))
            if cl is None:
                continue
            bars.append({"o": _f(bar.get("open_price")), "h": _f(bar.get("high_price")),
                         "l": _f(bar.get("low_price")), "c": cl, "v": _i(bar.get("volume")) or 0})
        self._bars_cache[underlying] = (time.time(), bars)
        return bars

    # --- option chain (entry screening) ----------------------------------------------------------
    async def get_chain(self, underlying: str) -> list[OptionContractQuote]:
        c = self._chain_cache.get(underlying)
        if c and time.time() - c[0] < self._chain_ttl:
            return c[1]
        spot = await self.get_underlying_price(underlying)
        try:
            ch = await self._call("get_option_chains", {"underlying_symbol": underlying})
        except Exception:  # noqa: BLE001
            return self._chain_cache.get(underlying, (0, []))[1]
        chains = (ch.get("data") or {}).get("chains") or []
        exps = chains[0].get("expiration_dates", []) if chains else []
        today = date.today()
        target = [e for e in exps if 0 < (date.fromisoformat(e) - today).days <= self._dte_window]

        id_meta: dict[str, tuple[float, date, str]] = {}
        for exp in target:
            for typ in ("put", "call"):
                try:
                    r = await self._call("get_option_instruments", {
                        "chain_symbol": underlying, "expiration_dates": exp,
                        "type": typ, "tradability": "tradable"})
                except Exception:  # noqa: BLE001
                    continue
                for it in self._b._iter_records(r):
                    k = _f(it.get("strike_price"))
                    iid = it.get("id")
                    if not iid or k is None:
                        continue
                    if spot:  # narrow to OTM-ish to cut quote volume (puts below, calls above)
                        if typ == "put" and k > spot * 1.05:
                            continue
                        if typ == "call" and k < spot * 0.95:
                            continue
                    id_meta[str(iid)] = (k, date.fromisoformat(exp), typ)

        out: list[OptionContractQuote] = []
        ids = list(id_meta)
        for i in range(0, len(ids), 40):
            try:
                r = await self._call("get_option_quotes", {"instrument_ids": ids[i:i + 40]})
            except Exception:  # noqa: BLE001
                continue
            for rec in self._b._iter_records(r):
                qd = rec.get("quote") or {}
                meta = id_meta.get(str(qd.get("instrument_id") or ""))
                if not meta:
                    continue
                strike, exp, typ = meta
                out.append(OptionContractQuote(
                    occ_symbol=_occ(underlying, exp, typ, strike), underlying=underlying,
                    option_id=str(qd.get("instrument_id")), option_type=typ, strike=strike,
                    expiration=exp, bid=_f(qd.get("bid_price")), ask=_f(qd.get("ask_price")),
                    mark=_f(qd.get("adjusted_mark_price")) or _f(qd.get("mark_price")),
                    delta=_f(qd.get("delta")), iv=_f(qd.get("implied_volatility")),
                    theta=_f(qd.get("theta")), gamma=_f(qd.get("gamma")), vega=_f(qd.get("vega")),
                    open_interest=_i(qd.get("open_interest")), volume=_i(qd.get("volume"))))
        self._chain_cache[underlying] = (time.time(), out)
        return out

    # --- held-contract quote (close side) --------------------------------------------------------
    async def get_quote(self, position: Position) -> OptionQuote | None:
        p = _parse_occ(position.occ_symbol)
        if not p:
            return None
        root, exp, typ, strike = p
        try:
            r = await self._call("get_option_instruments", {
                "chain_symbol": root, "expiration_dates": exp.isoformat(),
                "strike_price": str(strike), "type": typ, "tradability": "tradable"})
        except Exception:  # noqa: BLE001
            return None
        insts = self._b._iter_records(r)
        iid = str(insts[0].get("id")) if insts else None
        if not iid:
            return None
        try:
            q = await self._call("get_option_quotes", {"instrument_ids": [iid]})
        except Exception:  # noqa: BLE001
            return None
        recs = self._b._iter_records(q)
        qd = (recs[0].get("quote") or {}) if recs else {}
        return OptionQuote(
            occ_symbol=position.occ_symbol, bid=_f(qd.get("bid_price")), ask=_f(qd.get("ask_price")),
            mark=_f(qd.get("adjusted_mark_price")) or _f(qd.get("mark_price")),
            delta=_f(qd.get("delta")), iv=_f(qd.get("implied_volatility")))
