"""Company-data provider: fundamentals/financials parsing, derivation, fail-open, build + cache.

Fixtures mirror the REAL Robinhood MCP payloads captured live 2026-09-07 (results is a positional
list aligned to requested symbols; get_financials returns newest-first rows with dollar strings)."""
import json

import pytest

from agentic.config import Settings
from agentic.marketdata.company_data import (
    BrokerCompanyDataProvider,
    CompanyProfile,
    NullCompanyDataProvider,
    build_company_data,
    merge_financials,
    parse_fundamentals,
)

# get_equity_fundamentals: data.results is positional (one entry per requested symbol).
FUND = {"data": {"results": [{
    "symbol": "AAA", "sector": "Technology Services",
    "market_cap": "49279796282.199997", "pe_ratio": "28.500000",
    "pb_ratio": "9.800430", "dividend_yield": "0.400000",
}]}, "guide": "Symbols that did not resolve..."}

# get_financials period="annual": results[0] wraps a newest-first `financials` list.
FIN = {"data": {"results": [{
    "symbol": "AAA", "period": "annual", "financials": [
        {"fiscal_year": 2025, "period_end_date": "2025-12-31", "revenue": "1200000000.000000",
         "gross_profit": "720000000.000000", "net_income": "180000000.000000", "net_margin": "15.0"},
        {"fiscal_year": 2024, "period_end_date": "2024-12-31", "revenue": "1000000000.000000",
         "gross_profit": "600000000.000000", "net_income": "140000000.000000", "net_margin": "14.0"},
        {"fiscal_year": 2023, "period_end_date": "2023-12-31", "revenue": "820000000.000000",
         "gross_profit": "480000000.000000", "net_income": "90000000.000000", "net_margin": "11.0"},
    ],
}]}, "guide": "results is positional..."}


def test_parse_fundamentals_basic():
    p = parse_fundamentals(FUND, "aaa")
    assert p.symbol == "AAA"
    assert p.sector == "Technology Services"
    assert p.market_cap == pytest.approx(4.92797962822e10)
    assert p.pe_ratio == pytest.approx(28.5)
    assert p.dividend_yield == pytest.approx(0.4)


def test_parse_fundamentals_tolerates_junk():
    p = parse_fundamentals({"data": {"results": []}}, "zzz")
    assert p.symbol == "ZZZ"
    assert p.market_cap is None and p.pe_ratio is None
    # totally malformed payloads still yield a bare profile, never raise
    assert parse_fundamentals("not json", "zzz").symbol == "ZZZ"
    assert parse_fundamentals(None, "zzz").symbol == "ZZZ"


def test_merge_financials_margins_and_growth():
    p = merge_financials(parse_fundamentals(FUND, "aaa"), FIN)
    # latest period: gross_margin = 720M/1200M = 0.60; net_margin = 180M/1200M = 0.15
    assert p.gross_margin == pytest.approx(0.60)
    assert p.net_margin == pytest.approx(0.15)
    # revenue CAGR over ~2yr span: (1200/820)**(1/2) - 1 ~= 0.21
    assert p.revenue_growth == pytest.approx(0.21, abs=0.01)
    # RH gives no assets/COGS/FCF -> these stay None (Phase B / EDGAR)
    assert p.gross_profitability is None and p.fcf_margin is None and p.operating_margin is None


def test_merge_financials_partial_row():
    # a single row with revenue + gross_profit but no net_income -> gross_margin only, no growth
    fin = {"data": {"results": [{"period": "annual", "financials": [
        {"period_end_date": "2025-12-31", "revenue": "100", "gross_profit": "40"}]}]}}
    p = merge_financials(CompanyProfile(symbol="AAA"), fin)
    assert p.gross_margin == pytest.approx(0.40)
    assert p.net_margin is None and p.revenue_growth is None  # one row -> no CAGR


def test_merge_financials_empty_is_noop():
    p0 = parse_fundamentals(FUND, "aaa")
    p1 = merge_financials(parse_fundamentals(FUND, "aaa"),
                          {"data": {"results": [{"period": "annual", "financials": []}]}})
    assert p1.gross_margin is None and p1.net_margin is None and p1.revenue_growth is None
    assert p1.market_cap == p0.market_cap  # fundamentals untouched


def test_merge_financials_negative_gross_profit():
    # pre-revenue / loss-making name: negative gross profit -> negative gross_margin (SMR-like)
    fin = {"data": {"results": [{"period": "annual", "financials": [
        {"period_end_date": "2025-12-31", "revenue": "75000", "gross_profit": "-152000",
         "net_income": "-47539000"}]}]}}
    p = merge_financials(CompanyProfile(symbol="SMR"), fin)
    assert p.gross_margin < 0


# --- BrokerCompanyDataProvider ------------------------------------------------------------------

class _FakeBroker:
    """Dispatches _call_tool by tool name to canned fundamentals/financials payloads."""
    def __init__(self, tools, *, fund=None, fin=None, raise_exc=None, connected=True):
        self._connected = connected
        self._tools = list(tools)
        self._fund = fund
        self._fin = fin
        self._raise = raise_exc
        self.calls: list[tuple[str, dict]] = []

    async def _call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        if "fundamental" in name:
            return self._fund
        if "financ" in name:
            return self._fin
        return {}


@pytest.mark.asyncio
async def test_broker_provider_reads_merges_and_caches():
    broker = _FakeBroker(["get_equity_fundamentals", "get_financials"], fund=FUND, fin=FIN)
    prov = BrokerCompanyDataProvider(broker)
    assert prov._fund_tool == "get_equity_fundamentals"
    assert prov._fin_tool == "get_financials"
    p = await prov.profile("aaa")
    assert p is not None and p.sector == "Technology Services"
    assert p.gross_margin == pytest.approx(0.60)
    assert p.revenue_growth == pytest.approx(0.21, abs=0.01)
    # verified live: symbols is a positional LIST; financials is fetched annual
    assert broker.calls == [
        ("get_equity_fundamentals", {"symbols": ["AAA"]}),
        ("get_financials", {"symbols": ["AAA"], "period": "annual"}),
    ]
    await prov.profile("AAA")  # cached -> no more calls
    assert len(broker.calls) == 2


@pytest.mark.asyncio
async def test_broker_provider_text_payload():
    broker = _FakeBroker(["get_equity_fundamentals"], fund=json.dumps(FUND))
    p = await BrokerCompanyDataProvider(broker).profile("aaa")
    assert p is not None and p.market_cap == pytest.approx(4.92797962822e10)


@pytest.mark.asyncio
async def test_broker_provider_failopen_on_error():
    broker = _FakeBroker(["get_equity_fundamentals"], raise_exc=RuntimeError("boom"))
    assert await BrokerCompanyDataProvider(broker).profile("aaa") is None


@pytest.mark.asyncio
async def test_broker_provider_financials_error_keeps_fundamentals():
    # fundamentals ok, financials tool raises -> we keep the fundamentals-only profile
    class _B(_FakeBroker):
        async def _call_tool(self, name, arguments):
            self.calls.append((name, arguments))
            if "financ" in name:
                raise RuntimeError("fin boom")
            return self._fund
    broker = _B(["get_equity_fundamentals", "get_financials"], fund=FUND)
    p = await BrokerCompanyDataProvider(broker).profile("aaa")
    assert p is not None and p.market_cap == pytest.approx(4.92797962822e10)
    assert p.gross_margin is None  # financials never merged


@pytest.mark.asyncio
async def test_broker_provider_no_fundamentals_tool():
    prov = BrokerCompanyDataProvider(_FakeBroker(["get_option_chains"]))
    assert prov._fund_tool is None
    assert await prov.profile("aaa") is None


# --- build_company_data -------------------------------------------------------------------------

def test_build_failopen_when_gate_off():
    assert isinstance(build_company_data(Settings(entry={"quality_scoring": False})),
                      NullCompanyDataProvider)


def test_build_failopen_no_broker():
    assert isinstance(build_company_data(Settings(entry={"quality_scoring": True})),
                      NullCompanyDataProvider)


def test_build_prefers_connected_broker():
    broker = _FakeBroker(["get_equity_fundamentals", "get_financials"], fund=FUND, fin=FIN)
    prov = build_company_data(Settings(entry={"quality_scoring": True}), broker)
    assert isinstance(prov, BrokerCompanyDataProvider)


def test_build_skips_broker_without_fundamentals_tool():
    broker = _FakeBroker(["get_option_chains"])
    assert isinstance(build_company_data(Settings(entry={"quality_scoring": True}), broker),
                      NullCompanyDataProvider)


def test_build_skips_unconnected_broker():
    broker = _FakeBroker(["get_equity_fundamentals"], connected=False)
    assert isinstance(build_company_data(Settings(entry={"quality_scoring": True}), broker),
                      NullCompanyDataProvider)
