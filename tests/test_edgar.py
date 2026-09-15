"""SEC EDGAR (Phase B): companyfacts parsing, Form-4 buy detection, CIK map, enrich orchestration.

Fixtures mirror the real EDGAR shapes captured live 2026-09-07 (companyfacts.facts.us-gaap.<TAG>.
units.USD[] rows; Form 4 primary XML with <transactionCode>). No network — a fake async client
serves canned payloads by URL."""
import pytest

from agentic.config import Settings
from agentic.domain.models import utcnow
from agentic.marketdata.company_data import CompanyProfile, EnrichedCompanyDataProvider
from agentic.marketdata.edgar import (
    EdgarClient,
    build_edgar_client,
    form4_buy_count,
    parse_facts,
    _cik_map,
)


def _usd(val, end="2024-12-31"):
    return {"units": {"USD": [{"form": "10-K", "fp": "FY", "val": val, "end": end}]}}


FACTS = {"facts": {"us-gaap": {
    "Revenues": _usd(1000),
    "CostOfGoodsAndServicesSold": _usd(600),
    "Assets": _usd(2000),
    "NetCashProvidedByUsedInOperatingActivities": _usd(300),
    "PaymentsToAcquirePropertyPlantAndEquipment": _usd(100),
}}}

FORM4_BUY = "<?xml version='1.0'?><ownershipDocument><nonDerivativeTransaction>" \
            "<transactionCode>P</transactionCode>" \
            "<transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>" \
            "</nonDerivativeTransaction></ownershipDocument>"
FORM4_SELL = "<?xml version='1.0'?><ownershipDocument><nonDerivativeTransaction>" \
             "<transactionCode>S</transactionCode></nonDerivativeTransaction></ownershipDocument>"
FORM4_AWARD = "<ownershipDocument><transactionCode>A</transactionCode></ownershipDocument>"


def test_parse_facts_fcf_and_gross_profitability():
    out = parse_facts(FACTS)
    # FCF margin = (OCF 300 - capex 100) / revenue 1000 = 0.20
    assert out["fcf_margin"] == pytest.approx(0.20)
    # gross profitability = (revenue 1000 - COGS 600) / assets 2000 = 0.20
    assert out["gross_profitability"] == pytest.approx(0.20)


def test_parse_facts_uses_fallback_tags():
    facts = {"facts": {"us-gaap": {
        "RevenueFromContractWithCustomerExcludingAssessedTax": _usd(5000),
        "CostOfGoodsAndServicesSold": _usd(1500),
        "Assets": _usd(49000),
        "NetCashProvidedByUsedInOperatingActivities": _usd(3000),
        "PaymentsToAcquireProductiveAssets": _usd(10000),
    }}}
    out = parse_facts(facts)
    # CRWV-like: FCF margin = (3000 - 10000)/5000 = -1.4 (deep cash burn)
    assert out["fcf_margin"] == pytest.approx(-1.4)
    assert out["gross_profitability"] == pytest.approx((5000 - 1500) / 49000, abs=1e-4)


def test_parse_facts_partial_and_junk():
    # missing capex -> no fcf_margin; missing assets -> no gross_profitability; never raises
    assert "fcf_margin" not in parse_facts({"facts": {"us-gaap": {
        "Revenues": _usd(1000), "NetCashProvidedByUsedInOperatingActivities": _usd(300)}}})
    assert parse_facts({}) == {}
    assert parse_facts(None) == {}


def test_form4_buy_count():
    assert form4_buy_count(FORM4_BUY) == 1
    assert form4_buy_count(FORM4_SELL) == 0
    assert form4_buy_count(FORM4_AWARD) == 0
    assert form4_buy_count(FORM4_BUY + FORM4_BUY) == 2
    assert form4_buy_count(None) == 0


def test_cik_map():
    m = _cik_map({"0": {"cik_str": 37996, "ticker": "F", "title": "Ford"},
                  "1": {"cik_str": 320193, "ticker": "aapl", "title": "Apple"}})
    assert m["F"] == "0000037996"
    assert m["AAPL"] == "0000320193"


def test_build_edgar_client():
    assert build_edgar_client(None) is None
    assert build_edgar_client("  ") is None
    assert isinstance(build_edgar_client("Me me@example.com"), EdgarClient)


# --- enrich orchestration via a fake async client -----------------------------------------------

class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class _FakeClient:
    """Serves canned responses by URL substring; async context manager like httpx.AsyncClient."""
    def __init__(self, routes):
        self._routes = routes
        self.gets: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        self.gets.append(url)
        for frag, resp in self._routes.items():
            if frag in url:
                return resp
        return _FakeResp(404)


class _FakeEdgar(EdgarClient):
    def __init__(self, routes):
        super().__init__("Test me@example.com")
        self._routes = routes

    def _client(self):
        return _FakeClient(self._routes)


@pytest.mark.asyncio
async def test_enrich_populates_fields():
    today = utcnow().date().isoformat()
    subs = {"filings": {"recent": {
        "form": ["4", "4", "10-K"],
        "filingDate": [today, today, "2020-01-01"],
        "accessionNumber": ["0000037996-26-000178", "0000037996-26-000179", "x"],
        "primaryDocument": ["xslF345X06/buy.xml", "xslF345X06/sell.xml", "x.htm"],
    }}}
    routes = {
        "company_tickers.json": _FakeResp(200, {"0": {"cik_str": 37996, "ticker": "F", "title": "Ford"}}),
        "companyfacts": _FakeResp(200, FACTS),
        "submissions": _FakeResp(200, subs),
        "/buy.xml": _FakeResp(200, text=FORM4_BUY),
        "/sell.xml": _FakeResp(200, text=FORM4_SELL),
    }
    prof = CompanyProfile(symbol="F")
    await _FakeEdgar(routes).enrich(prof)
    assert prof.fcf_margin == pytest.approx(0.20)
    assert prof.gross_profitability == pytest.approx(0.20)
    assert prof.insider_net_buys_90d == 1  # one Form 4 with a P-buy; the sell doesn't count


@pytest.mark.asyncio
async def test_enrich_skips_fcf_for_financials():
    # SOFI-like: a financial-sector SIC -> FCF / gross profitability are meaningless, skip them,
    # but still count insider buys.
    today = utcnow().date().isoformat()
    subs = {"sic": "6199", "sicDescription": "Finance Services", "filings": {"recent": {
        "form": ["4"], "filingDate": [today],
        "accessionNumber": ["0000000000-26-000001"], "primaryDocument": ["xslF345X06/buy.xml"],
    }}}
    routes = {
        "company_tickers.json": _FakeResp(200, {"0": {"cik_str": 1, "ticker": "SOFI", "title": "SoFi"}}),
        "companyfacts": _FakeResp(200, FACTS),  # would yield fcf/gp, but must be skipped
        "submissions": _FakeResp(200, subs),
        "/buy.xml": _FakeResp(200, text=FORM4_BUY),
    }
    prof = CompanyProfile(symbol="SOFI")
    await _FakeEdgar(routes).enrich(prof)
    assert prof.fcf_margin is None and prof.gross_profitability is None  # skipped for financials
    assert prof.insider_net_buys_90d == 1                                # insider still counted


def test_is_financial():
    from agentic.marketdata.edgar import _is_financial
    assert _is_financial("6199") and _is_financial(6021) and _is_financial("6798")
    assert not _is_financial("3711") and not _is_financial(None) and not _is_financial("tech")


@pytest.mark.asyncio
async def test_enrich_failopen_unknown_ticker():
    routes = {"company_tickers.json": _FakeResp(200, {"0": {"cik_str": 1, "ticker": "ZZZ", "title": "Z"}})}
    prof = CompanyProfile(symbol="NOPE")
    await _FakeEdgar(routes).enrich(prof)  # ticker not in map -> no-op, never raises
    assert prof.fcf_margin is None and prof.insider_net_buys_90d is None


@pytest.mark.asyncio
async def test_enriched_provider_merges_base_and_edgar():
    class _Base:
        async def profile(self, symbol):
            return CompanyProfile(symbol=symbol.upper(), gross_margin=0.72, sector="Tech")

    class _Enricher:
        async def enrich(self, prof):
            prof.fcf_margin = -1.4
            prof.gross_profitability = 0.075

    p = await EnrichedCompanyDataProvider(_Base(), _Enricher()).profile("crwv")
    assert p.gross_margin == pytest.approx(0.72)   # from base (RH)
    assert p.fcf_margin == pytest.approx(-1.4)     # from enricher (EDGAR)


@pytest.mark.asyncio
async def test_enriched_provider_failopen_when_both_empty():
    class _NullBase:
        async def profile(self, symbol):
            return None

    class _NoopEnricher:
        async def enrich(self, prof):
            pass

    assert await EnrichedCompanyDataProvider(_NullBase(), _NoopEnricher()).profile("x") is None


def test_build_company_data_wraps_with_edgar():
    from agentic.marketdata.company_data import build_company_data
    s = Settings(entry={"quality_scoring": True, "quality_use_edgar": True,
                        "edgar_user_agent": "Me me@example.com"})
    prov = build_company_data(s, broker=None)
    assert isinstance(prov, EnrichedCompanyDataProvider)


def test_build_company_data_no_edgar_without_ua():
    from agentic.marketdata.company_data import build_company_data, NullCompanyDataProvider
    s = Settings(entry={"quality_scoring": True, "quality_use_edgar": True})  # no UA
    prov = build_company_data(s, broker=None)
    assert isinstance(prov, NullCompanyDataProvider)  # EDGAR skipped, no broker -> Null base
