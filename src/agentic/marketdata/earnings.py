"""Earnings-date provider — READ-ONLY, decoupled from order routing.

Sources the next earnings date per symbol from the Robinhood agentic MCP (`get_earnings_results`)
so the scanner never sells a put that would be held through an earnings report. Never places orders.

Two backends, preferred in order (see ``build_earnings_provider``):
  1. ``BrokerEarningsProvider`` — REUSES an already-connected RH MCP broker's OAuth session. The
     broker's connect() probe already exposes the earnings tools, so this is a single auth path
     with no second connection and no legacy token. This is the default now the broker runs OAuth.
  2. ``RHEarningsProvider`` — legacy standalone session on a static ``ROBINHOOD_MCP_TOKEN`` bearer,
     used only when that env var is set (e.g. token override / no OAuth broker).

Fail-open everywhere: no source / no 'mcp' extra / any error → next_earnings returns None and
nothing is gated. Alpaca has no earnings endpoint (corporate actions = dividends/splits only), so
the RH MCP is the source.
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date
from typing import Protocol

from ..config import Settings, get_secret
from ..domain.models import utcnow

log = logging.getLogger("agentic.earnings")

MCP_URL = "https://agent.robinhood.com/mcp/trading"


class EarningsProvider(Protocol):
    async def next_earnings(self, symbol: str) -> date | None: ...


class NullEarningsProvider:
    """No earnings source — the gate is a no-op (fail-open)."""
    async def next_earnings(self, symbol: str) -> date | None:
        return None


def parse_next_earnings(results: list[dict], today: date) -> date | None:
    """Earliest upcoming report date from a get_earnings_results payload (>= today)."""
    best: date | None = None
    for r in results:
        rep = (r.get("report") or {}) if isinstance(r, dict) else {}
        ds = rep.get("date")
        if not ds:
            continue
        try:
            d = date.fromisoformat(ds)
        except (ValueError, TypeError):
            continue
        if d >= today and (best is None or d < best):
            best = d
    return best


def earnings_blackout(
    earnings_date: date | None, today: date, dte_max: int, buffer_days: int
) -> bool:
    """True if a put opened now (up to dte_max) could be held through earnings, plus a buffer.

    Require earnings to be MORE than (dte_max + buffer_days) away; otherwise skip the name.
    """
    if earnings_date is None:
        return False
    return (earnings_date - today).days <= dte_max + buffer_days


def _select_earnings_tool(tools: list[str]) -> str | None:
    """Pick the get_earnings_results-style tool from a probed MCP tool list (else None)."""
    lowered = {t.lower(): t for t in tools}
    for pref in ("get_earnings_results", "earnings_results"):
        if pref in tools:
            return pref
    for low, orig in lowered.items():
        if "earnings" in low and "result" in low:
            return orig
    for low, orig in lowered.items():
        if "earnings" in low:
            return orig
    return None


class BrokerEarningsProvider:
    """Reads next earnings per symbol by REUSING an already-connected RH MCP broker's OAuth
    session — the durable single-auth-path source. The broker's connect() probe already exposes
    the earnings tools, so this needs no second connection and no legacy ROBINHOOD_MCP_TOKEN. It
    delegates to the broker's ``_call_tool`` (which serializes on the shared OAuth provider/lock,
    so it never races the refresh-token rotation). Fail-open: missing tool / call error → None."""

    def __init__(self, broker: object, tool: str | None = None):
        self._broker = broker
        self._tool = tool or _select_earnings_tool(list(getattr(broker, "_tools", None) or []))
        self._cache: dict[tuple[str, date], date | None] = {}

    async def next_earnings(self, symbol: str) -> date | None:
        call = getattr(self._broker, "_call_tool", None)
        if not self._tool or not callable(call):
            return None
        today = utcnow().date()
        key = (symbol.upper(), today)
        if key in self._cache:
            return self._cache[key]
        val: date | None = None
        try:
            raw = await call(self._tool, {"symbol": symbol.upper()})
            if isinstance(raw, str):
                raw = json.loads(raw) if raw.strip() else {}
            data = raw.get("data", raw) if isinstance(raw, dict) else {}
            results = data.get("results", []) if isinstance(data, dict) else []
            val = parse_next_earnings(results, today)
        except Exception as exc:  # noqa: BLE001 — earnings is advisory; never break the scan
            log.warning("Earnings lookup failed for %s: %s", symbol, exc)
            val = None
        self._cache[key] = val
        return val


class RHEarningsProvider:
    """Reads next earnings per symbol via the RH MCP get_earnings_results tool (read-only)."""

    def __init__(self, url: str = MCP_URL):
        self.url = url
        self._token = get_secret("ROBINHOOD_MCP_TOKEN")
        self._tool: str | None = None
        self._cache: dict[tuple[str, date], date | None] = {}

    @asynccontextmanager
    async def _session(self):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        headers = {"Authorization": f"Bearer {self._token}"}
        async with streamablehttp_client(self.url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def _resolve_tool(self, session) -> str | None:
        if self._tool:
            return self._tool
        names = [t.name for t in (await session.list_tools()).tools]
        for pref in ("get_earnings_results", "earnings_results"):
            if pref in names:
                self._tool = pref
                return pref
        self._tool = next((n for n in names if "earnings" in n.lower()), None)
        return self._tool

    async def next_earnings(self, symbol: str) -> date | None:
        if not self._token:
            return None
        today = utcnow().date()
        key = (symbol.upper(), today)
        if key in self._cache:
            return self._cache[key]

        val: date | None = None
        try:
            async with self._session() as session:
                tool = await self._resolve_tool(session)
                if tool is None:
                    self._cache[key] = None
                    return None
                res = await session.call_tool(tool, arguments={"symbol": symbol.upper()})
                raw = getattr(res, "structuredContent", None)
                if raw is None:
                    texts = "".join(getattr(c, "text", "") for c in getattr(res, "content", []))
                    raw = json.loads(texts) if texts else {}
                data = raw.get("data", raw) if isinstance(raw, dict) else {}
                results = data.get("results", []) if isinstance(data, dict) else []
                val = parse_next_earnings(results, today)
        except Exception as exc:  # noqa: BLE001 — earnings is advisory; never break the scan
            log.warning("Earnings lookup failed for %s: %s", symbol, exc)
            val = None
        self._cache[key] = val
        return val


def build_earnings_provider(settings: Settings, broker: object | None = None) -> EarningsProvider:
    """Earnings source for the blackout gate, in preference order:

    1. Reuse a connected RH MCP broker's OAuth session (single auth path — no 2nd connection, no
       legacy token). The durable default now the broker runs on OAuth.
    2. Legacy standalone provider on a static ``ROBINHOOD_MCP_TOKEN`` bearer, if that env is set.
    3. No-op / fail-open (gate inactive) otherwise.
    """
    if not settings.entry.earnings_gate:
        return NullEarningsProvider()
    # (1) Preferred: the live broker already exposes the earnings tools over OAuth.
    if broker is not None and getattr(broker, "_connected", False):
        tool = _select_earnings_tool(list(getattr(broker, "_tools", None) or []))
        if tool and callable(getattr(broker, "_call_tool", None)):
            log.info("earnings_gate: using the connected broker's OAuth session (tool=%s).", tool)
            return BrokerEarningsProvider(broker, tool)
    try:
        import mcp  # noqa: F401
    except ImportError:
        log.warning("earnings_gate on but the 'mcp' extra isn't installed — gate inactive.")
        return NullEarningsProvider()
    # (2) Legacy standalone token path.
    if get_secret("ROBINHOOD_MCP_TOKEN"):
        return RHEarningsProvider()
    # (3) No usable source.
    log.info("earnings_gate on but no earnings source (broker lacks earnings tools / not "
             "connected, and ROBINHOOD_MCP_TOKEN unset) — gate inactive (fail-open).")
    return NullEarningsProvider()
