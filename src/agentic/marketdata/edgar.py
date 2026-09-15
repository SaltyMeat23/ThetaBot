"""SEC EDGAR — free, official company data the Robinhood MCP doesn't expose (Phase B).

Enriches a CompanyProfile with the pieces RH lacks:
  * **free cash flow margin** (OCF - capex) / revenue — arms the cash-burn junk screen,
  * **gross profitability** (revenue - COGS) / assets — the Novy-Marx quality measure (needs assets),
  * **insider open-market buys** in the last 90 days (Form 4, transaction code "P") — a documented,
    genuinely non-financial "alpha" signal (executives buying their own stock with conviction).

All data is free from https://data.sec.gov and https://www.sec.gov/Archives. SEC requires a
descriptive User-Agent with a contact (see ``edgar_user_agent`` config); a bot-ish UA gets a 403.
Fail-open throughout: no UA / any error / missing facts → the profile is left as-is and nothing is
gated. Financial figures use ``companyfacts`` (one call, all tags, tolerant of per-company tag
variation) rather than the flaky per-concept endpoint. Verified against live EDGAR 2026-09-07.
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta

from ..domain.models import utcnow
from .company_data import CompanyProfile

log = logging.getLogger("agentic.edgar")

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_SUBS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
_ARCHIVE = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc}/{doc}"

# Candidate us-gaap tags per concept, in preference order (companies tag inconsistently).
_FACT_TAGS = {
    "revenue": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "cogs": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold"],
    "assets": ["Assets"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
              "PaymentsForCapitalImprovements"],
}
_MAX_FORM4_FETCHES = 20  # bound per-name Form-4 document fetches (cluster signal, not a census)


def _latest_annual(us_gaap: dict, tags: list[str]) -> float | None:
    """Latest annual (10-K, full-year) USD value across candidate tags, or None."""
    for tag in tags:
        arr = (us_gaap.get(tag, {}) or {}).get("units", {}).get("USD")
        if not isinstance(arr, list):
            continue
        ann = [e for e in arr if e.get("form") == "10-K" and e.get("fp") == "FY"
               and e.get("val") is not None and e.get("end")]
        if ann:
            ann.sort(key=lambda e: e["end"])
            try:
                return float(ann[-1]["val"])
            except (TypeError, ValueError):
                continue
    return None


def parse_facts(facts_json: dict | None) -> dict:
    """Derive {fcf_margin, gross_profitability} from a companyfacts payload (pure; fail-open)."""
    us = ((facts_json or {}).get("facts", {}) or {}).get("us-gaap", {})
    if not isinstance(us, dict) or not us:
        return {}
    rev = _latest_annual(us, _FACT_TAGS["revenue"])
    cogs = _latest_annual(us, _FACT_TAGS["cogs"])
    assets = _latest_annual(us, _FACT_TAGS["assets"])
    ocf = _latest_annual(us, _FACT_TAGS["ocf"])
    capex = _latest_annual(us, _FACT_TAGS["capex"])
    out: dict = {}
    # FCF = operating cash flow - capex (capex is reported as a positive outflow; use magnitude).
    if ocf is not None and capex is not None and rev and rev > 0:
        out["fcf_margin"] = round((ocf - abs(capex)) / rev, 4)
    # Novy-Marx gross profitability = (revenue - COGS) / total assets.
    if rev is not None and cogs is not None and assets and assets > 0:
        out["gross_profitability"] = round((rev - cogs) / assets, 4)
    return out


def _is_financial(sic: object) -> bool:
    """True for financial-sector SIC codes (6000-6999: banks, lenders, insurers, REITs), where
    OCF-minus-capex FCF and gross-profitability-on-assets are not meaningful measures."""
    try:
        return 6000 <= int(str(sic)) < 7000
    except (TypeError, ValueError):
        return False


def form4_buy_count(xml_text: str | None) -> int:
    """# of open-market purchase transactions (code "P") in a Form 4 primary XML (pure)."""
    return len(re.findall(r"<transactionCode>\s*P\s*</transactionCode>", xml_text or ""))


def _cik_map(tickers_json: dict) -> dict[str, str]:
    """Build {TICKER: zero-padded-10-digit CIK} from company_tickers.json (pure)."""
    out: dict[str, str] = {}
    for row in (tickers_json or {}).values():
        if isinstance(row, dict) and row.get("ticker") and row.get("cik_str") is not None:
            out[str(row["ticker"]).upper()] = str(row["cik_str"]).zfill(10)
    return out


class EdgarClient:
    """Async EDGAR reader: resolves CIK, fetches companyfacts + insider buys. Fail-open, cached
    per (symbol, day) so a watchlist name costs one round of calls per day."""

    def __init__(self, user_agent: str, max_form4: int = _MAX_FORM4_FETCHES):
        self._ua = user_agent
        self._max_form4 = max_form4
        self._cik: dict[str, str] | None = None
        self._facts_cache: dict[tuple[str, date], dict] = {}
        self._buys_cache: dict[tuple[str, date], int | None] = {}
        self._subs_cache: dict[tuple[str, date], dict] = {}

    def _client(self):
        import httpx
        return httpx.AsyncClient(
            headers={"User-Agent": self._ua, "Accept-Encoding": "gzip, deflate"}, timeout=25.0)

    async def _cik_for(self, client, symbol: str) -> str | None:
        if self._cik is None:
            r = await client.get(_TICKERS_URL)
            self._cik = _cik_map(r.json()) if r.status_code == 200 else {}
        return self._cik.get(symbol.upper())

    async def enrich(self, profile: CompanyProfile) -> None:
        """Overlay fcf_margin / gross_profitability / insider_net_buys_90d onto profile (in place)."""
        today = utcnow().date()
        sym = profile.symbol.upper()
        try:
            import httpx  # noqa: F401
        except ImportError:
            log.warning("EDGAR enrichment needs the 'web' extra (httpx) — skipping.")
            return
        try:
            async with self._client() as client:
                cik = await self._cik_for(client, sym)
                if not cik:
                    return
                # Submissions once — carries the SIC and the recent-filings list (insider Form 4s).
                skey = (sym, today)
                if skey not in self._subs_cache:
                    sr = await client.get(_SUBS_URL.format(cik=cik))
                    self._subs_cache[skey] = sr.json() if sr.status_code == 200 else {}
                subs = self._subs_cache[skey]
                # Facts-derived FCF / gross profitability are meaningless for FINANCIALS (banks,
                # lenders, insurers, REITs — SIC 6000-6999): OCF-capex and GP/assets don't model a
                # balance-sheet business. Skip them there; the RH margins still drive profitability.
                fkey = (sym, today)
                if fkey not in self._facts_cache:
                    fr = await client.get(_FACTS_URL.format(cik=cik))
                    self._facts_cache[fkey] = parse_facts(fr.json()) if fr.status_code == 200 else {}
                facts = self._facts_cache[fkey]
                if not _is_financial(subs.get("sic")):
                    if facts.get("fcf_margin") is not None:
                        profile.fcf_margin = facts["fcf_margin"]
                    if facts.get("gross_profitability") is not None:
                        profile.gross_profitability = facts["gross_profitability"]
                bkey = (sym, today)
                if bkey not in self._buys_cache:
                    self._buys_cache[bkey] = await self._insider_buys(client, cik, today, subs)
                if self._buys_cache[bkey] is not None:
                    profile.insider_net_buys_90d = self._buys_cache[bkey]
        except Exception as exc:  # noqa: BLE001 — EDGAR is advisory; never break the scan
            log.warning("EDGAR enrichment failed for %s: %s", sym, exc)

    async def _insider_buys(self, client, cik: str, today: date, subs: dict) -> int | None:
        """Count insiders with an open-market buy (Form 4, code P) in the last 90 days."""
        if not subs:
            return None
        rec = (subs.get("filings", {}) or {}).get("recent", {})
        forms = rec.get("form", []) or []
        dates = rec.get("filingDate", []) or []
        accs = rec.get("accessionNumber", []) or []
        docs = rec.get("primaryDocument", []) or []
        cutoff = today - timedelta(days=90)
        cik_int = int(cik)
        buyers = 0
        fetched = 0
        for i, form in enumerate(forms):
            if form != "4" or i >= len(dates):
                continue
            try:
                if date.fromisoformat(dates[i]) < cutoff:
                    break  # recent[] is newest-first; once past the window we're done
            except (ValueError, TypeError):
                continue
            if fetched >= self._max_form4:
                break
            acc = accs[i].replace("-", "") if i < len(accs) else None
            doc = docs[i].split("/")[-1] if i < len(docs) and docs[i] else None
            if not acc or not doc:
                continue
            fetched += 1
            try:
                dr = await client.get(_ARCHIVE.format(cik_int=cik_int, acc=acc, doc=doc))
                if dr.status_code == 200 and form4_buy_count(dr.text) > 0:
                    buyers += 1
            except Exception:  # noqa: BLE001 — one bad doc shouldn't abort the count
                continue
        return buyers


def build_edgar_client(user_agent: str | None) -> EdgarClient | None:
    """An EdgarClient if a real (non-empty) User-Agent is configured, else None (fail-open)."""
    ua = (user_agent or "").strip()
    if not ua:
        log.info("EDGAR enrichment requested but edgar_user_agent is unset — skipping (SEC "
                 "requires a descriptive User-Agent with a contact, e.g. 'MyApp me@example.com').")
        return None
    return EdgarClient(ua)
