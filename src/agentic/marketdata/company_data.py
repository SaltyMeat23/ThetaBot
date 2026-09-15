"""Company quality/growth data provider — READ-ONLY, decoupled from order routing.

Sources per-symbol *fundamentals* (profitability, growth, cash generation, size/valuation) so the
scanner can tilt toward the measurable traits long-run winners share and screen out the junk.
Because a CSP that gets assigned means *owning the stock*, the quality of the underlying is a real
input — this feeds ``scoring.quality.quality_growth_score`` and the ``quality_score`` gate.

IMPORTANT — honest scope. This is NOT a multibagger predictor. Winners are rare (Bessembinder: ~4%
of stocks create all net wealth; ~57% don't beat T-bills). The score only tilts + screens; the real
edge is diversification + discipline + measuring our own hit rate via the analytics flywheel.

Two backends, preferred in order (see ``build_company_data``), mirroring the earnings provider:
  1. ``BrokerCompanyDataProvider`` — REUSES an already-connected RH MCP broker's OAuth session
     (``get_equity_fundamentals`` + ``get_financials``). Single auth path, no second connection.
  2. ``NullCompanyDataProvider`` — no source → ``profile`` returns None and nothing is gated.

Fail-open everywhere: no source / any error / missing fields → None sub-fields, and a missing
profile never blocks the scan. RH fundamentals reliably carry market_cap/pe/sector/size; margins,
revenue-growth and FCF depend on what ``get_financials`` actually returns (verified at paper-run
time) — the score gracefully degrades to whatever fields are present, and EDGAR XBRL (Phase B) is
the fallback source for the ones RH omits.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol

# RH get_financials returns rows newest-first with dollar figures as strings; annual rows give a
# clean multi-year revenue series (for CAGR). Fields per row (verified live 2026-09-07):
#   fiscal_year, fiscal_quarter, period_end_date, revenue, gross_profit, net_income, net_margin
# RH does NOT expose COGS, total assets, operating margin, or cash flow (FCF) — those are Phase B
# (SEC EDGAR XBRL). get_equity_fundamentals gives market_cap, pe_ratio, dividend_yield, sector.

from ..config import Settings
from ..domain.models import utcnow

log = logging.getLogger("agentic.company_data")


@dataclass
class CompanyProfile:
    """Normalized company fundamentals. Every field is optional — the score uses what's present."""
    symbol: str
    sector: str | None = None
    market_cap: float | None = None
    pe_ratio: float | None = None
    dividend_yield: float | None = None
    # Profitability / quality (the Novy-Marx edge). gross_profitability = (rev - COGS) / assets.
    gross_profitability: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    roic: float | None = None
    # Cash generation — the most consistent trait of long-run compounders.
    fcf_margin: float | None = None
    # Growth durability.
    revenue_growth: float | None = None   # annualized revenue CAGR (fraction, 0.20 = +20%/yr)
    # Insider open-market buys in the last 90 days (SEC EDGAR Form 4, code "P"). Phase B.
    insider_net_buys_90d: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class CompanyDataProvider(Protocol):
    async def profile(self, symbol: str) -> CompanyProfile | None: ...


class NullCompanyDataProvider:
    """No company-data source — the quality gate is a no-op (fail-open)."""
    async def profile(self, symbol: str) -> CompanyProfile | None:
        return None


def _num(x: Any) -> float | None:
    """Coerce a possibly-string, possibly-None field to float; None on failure."""
    if x is None or isinstance(x, bool):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # drop NaN


def _first_record(payload: Any) -> dict:
    """Unwrap an RH MCP payload to the first per-symbol record dict (defensive on shape)."""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload) if payload.strip() else {}
        except (ValueError, TypeError):
            return {}
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data", payload)
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list) and results:
            return results[0] if isinstance(results[0], dict) else {}
        if isinstance(results, dict):
            return results
        return data
    if isinstance(data, list) and data:
        return data[0] if isinstance(data[0], dict) else {}
    return {}


def _pick(rec: dict, *keys: str) -> float | None:
    """First numeric value among candidate keys (case-insensitive), else None."""
    lower = {k.lower(): v for k, v in rec.items()} if isinstance(rec, dict) else {}
    for k in keys:
        if k in rec:
            v = _num(rec[k])
            if v is not None:
                return v
        lk = k.lower()
        if lk in lower:
            v = _num(lower[lk])
            if v is not None:
                return v
    return None


def parse_fundamentals(payload: Any, symbol: str) -> CompanyProfile:
    """Build a CompanyProfile from a get_equity_fundamentals payload (defensive; fail-open)."""
    rec = _first_record(payload)
    sector = None
    for k in ("sector", "Sector"):
        if isinstance(rec.get(k), str) and rec[k].strip():
            sector = rec[k].strip()
            break
    return CompanyProfile(
        symbol=symbol.upper(),
        sector=sector,
        market_cap=_pick(rec, "market_cap", "marketCap", "market_capitalization"),
        pe_ratio=_pick(rec, "pe_ratio", "peRatio", "pe"),
        dividend_yield=_pick(rec, "dividend_yield", "dividendYield"),
    )


def _financials_rows(payload: Any) -> list[dict]:
    """Extract the per-period financials rows (newest-first) from a get_financials payload."""
    rec = _first_record(payload)
    rows = rec.get("financials") if isinstance(rec, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _year_span(newest: dict, oldest: dict) -> float | None:
    """Years between two rows' period_end_dates (for annualizing revenue growth)."""
    try:
        t0 = date.fromisoformat(str(newest.get("period_end_date")))
        t1 = date.fromisoformat(str(oldest.get("period_end_date")))
    except (ValueError, TypeError):
        return None
    days = (t0 - t1).days
    return days / 365.25 if days > 0 else None


def merge_financials(profile: CompanyProfile, payload: Any) -> CompanyProfile:
    """Overlay margins + revenue growth from a get_financials payload (real RH shape, defensive).

    Rows are newest-first with dollar figures as strings. Derives:
      * gross_margin / net_margin from the latest period (gross_profit or net_income over revenue),
      * revenue_growth as the annualized CAGR across the available rows (uses period_end_date spans,
        so it works for both annual and quarterly payloads).
    FCF, total assets and operating margin aren't in RH's payload → left None (Phase B / EDGAR).
    """
    rows = _financials_rows(payload)
    if not rows:
        return profile
    latest = rows[0]
    rev0 = _num(latest.get("revenue"))
    if rev0 and rev0 > 0:
        gp0 = _num(latest.get("gross_profit"))
        ni0 = _num(latest.get("net_income"))
        if gp0 is not None:
            profile.gross_margin = round(gp0 / rev0, 4)
        if ni0 is not None:
            profile.net_margin = round(ni0 / rev0, 4)
    # Revenue growth (annualized CAGR) from oldest -> newest revenue with a valid span.
    revs = [(r, _num(r.get("revenue"))) for r in rows]
    revs = [(r, v) for r, v in revs if v is not None and v > 0]
    if len(revs) >= 2:
        (new_row, new_rev), (old_row, old_rev) = revs[0], revs[-1]
        years = _year_span(new_row, old_row)
        if years and years > 0:
            profile.revenue_growth = round((new_rev / old_rev) ** (1.0 / years) - 1.0, 4)
    return profile


def _select_tool(tools: list[str], *prefs: str, contains: tuple[str, ...] = ()) -> str | None:
    """Pick a tool from a probed MCP tool list: exact preference first, then substring match."""
    for pref in prefs:
        if pref in tools:
            return pref
    lowered = {t.lower(): t for t in tools}
    for low, orig in lowered.items():
        if all(sub in low for sub in contains):
            return orig
    return None


class BrokerCompanyDataProvider:
    """Reads per-symbol fundamentals by REUSING an already-connected RH MCP broker's OAuth session.

    Delegates to the broker's ``_call_tool`` (serialized on the shared OAuth lock, so it never races
    token rotation), exactly like ``BrokerEarningsProvider``. Per-(symbol, day) cache — fundamentals
    move slowly, so one fetch per name per day is plenty. Fail-open: missing tool / error → None."""

    def __init__(self, broker: object, fundamentals_tool: str | None = None,
                 financials_tool: str | None = None):
        tools = list(getattr(broker, "_tools", None) or [])
        self._broker = broker
        self._fund_tool = fundamentals_tool or _select_tool(
            tools, "get_equity_fundamentals", "equity_fundamentals",
            contains=("fundamental",))
        self._fin_tool = financials_tool or _select_tool(
            tools, "get_financials", "financials", contains=("financ",))
        self._cache: dict[tuple[str, date], CompanyProfile | None] = {}

    async def profile(self, symbol: str) -> CompanyProfile | None:
        call = getattr(self._broker, "_call_tool", None)
        if not callable(call) or not self._fund_tool:
            return None
        today = utcnow().date()
        key = (symbol.upper(), today)
        if key in self._cache:
            return self._cache[key]
        sym = symbol.upper()
        prof: CompanyProfile | None = None
        try:
            # RH tools take a POSITIONAL symbols LIST (verified live); a bare {"symbol": ...} errors.
            raw = await call(self._fund_tool, {"symbols": [sym]})
            prof = parse_fundamentals(raw, symbol)
            if self._fin_tool:
                try:
                    # Annual rows give a clean multi-year revenue series for the growth CAGR.
                    fin = await call(self._fin_tool, {"symbols": [sym], "period": "annual"})
                    prof = merge_financials(prof, fin)
                except Exception as exc:  # noqa: BLE001 — financials are a bonus; keep fundamentals
                    log.warning("Financials lookup failed for %s: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001 — company data is advisory; never break the scan
            log.warning("Company-data lookup failed for %s: %s", symbol, exc)
            prof = None
        self._cache[key] = prof
        return prof


class EnrichedCompanyDataProvider:
    """Wraps a base provider (RH fundamentals) and overlays a secondary enricher (SEC EDGAR).

    The base supplies sector/valuation/margins/growth; the enricher fills the fields RH lacks
    (FCF margin, gross profitability, insider buys). Fail-open on both sides: if the base returns
    None we still try the enricher on a bare profile, and any enricher error leaves the base intact.
    """

    def __init__(self, base: CompanyDataProvider, enricher: object):
        self._base = base
        self._enricher = enricher  # has async enrich(profile) -> None

    async def profile(self, symbol: str) -> CompanyProfile | None:
        base = await self._base.profile(symbol)
        prof = base if base is not None else CompanyProfile(symbol=symbol.upper())
        try:
            await self._enricher.enrich(prof)
        except Exception as exc:  # noqa: BLE001 — enrichment is advisory; keep the base profile
            log.warning("Company-data enrichment failed for %s: %s", symbol, exc)
        # If neither source populated anything, report nothing (fail-open, never penalize).
        if base is None and all(getattr(prof, f) is None for f in (
                "market_cap", "gross_margin", "net_margin", "revenue_growth",
                "fcf_margin", "gross_profitability", "insider_net_buys_90d")):
            return None
        return prof


def build_company_data(settings: Settings, broker: object | None = None) -> CompanyDataProvider:
    """Company-data source for the quality gate/score, in preference order:

    1. Reuse a connected RH MCP broker's OAuth session (single auth path). Default when the quality
       gate is on and the broker exposes the fundamentals tool. Optionally wrapped with SEC EDGAR
       enrichment (Phase B) when quality_use_edgar is on and a User-Agent is configured.
    2. No-op / fail-open (score inactive) otherwise.
    """
    if not settings.entry.quality_scoring:
        return NullCompanyDataProvider()
    base: CompanyDataProvider = NullCompanyDataProvider()
    if broker is not None and getattr(broker, "_connected", False):
        tools = list(getattr(broker, "_tools", None) or [])
        fund_tool = _select_tool(tools, "get_equity_fundamentals", "equity_fundamentals",
                                 contains=("fundamental",))
        if fund_tool and callable(getattr(broker, "_call_tool", None)):
            log.info("quality_scoring: using the connected broker's OAuth session (tool=%s).", fund_tool)
            base = BrokerCompanyDataProvider(broker, fund_tool)
    # Optional SEC EDGAR enrichment (FCF / gross profitability / insider buys).
    if settings.entry.quality_use_edgar:
        from .edgar import build_edgar_client  # local import avoids a module import cycle
        edgar = build_edgar_client(settings.entry.edgar_user_agent)
        if edgar is not None:
            log.info("quality_scoring: SEC EDGAR enrichment enabled.")
            return EnrichedCompanyDataProvider(base, edgar)
    if isinstance(base, NullCompanyDataProvider):
        log.info("quality_scoring on but no company-data source (broker not connected / lacks the "
                 "fundamentals tool) — score inactive (fail-open).")
    return base
