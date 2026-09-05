"""Earnings gate: payload parsing, blackout math, fail-open construction, scanner skip."""
from datetime import date, timedelta

import pytest

from agentic.config import Settings
from agentic.domain.models import utcnow
import json

from agentic.marketdata.earnings import (
    BrokerEarningsProvider,
    NullEarningsProvider,
    build_earnings_provider,
    earnings_blackout,
    parse_next_earnings,
)
from agentic.brokers.paper_broker import PaperBroker
from agentic.marketdata.base import PaperMarketData
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.entry_decisions import EntryDecisionStore

# Shape mirrors the live RH get_earnings_results payload (reported quarters + upcoming w/ null actual).
BULL_RESULTS = [
    {"report": {"date": "2025-11-20"}, "eps": {"actual": "0.070000"}},
    {"report": {"date": "2026-03-04"}, "eps": {"actual": "0.040000"}},
    {"report": {"date": "2026-05-21"}, "eps": {"actual": "0.030000"}},
    {"report": {"date": "2026-08-27"}, "eps": {"actual": None}},
    {"report": {"date": "2026-11-19"}, "eps": {"actual": None}},
]


def test_parse_next_picks_earliest_upcoming():
    assert parse_next_earnings(BULL_RESULTS, date(2026, 7, 6)) == date(2026, 8, 27)


def test_parse_next_none_when_all_past():
    assert parse_next_earnings(BULL_RESULTS, date(2027, 1, 1)) is None


def test_parse_next_tolerates_junk():
    assert parse_next_earnings([{"report": {}}, {"foo": 1}, {"report": {"date": "bad"}}],
                               date(2026, 7, 6)) is None


def test_blackout_math():
    today = date(2026, 7, 6)
    # earnings 5 days out, weeklies dte_max=14 + buffer 7 -> within 21 -> blackout
    assert earnings_blackout(today + timedelta(days=5), today, 14, 7) is True
    # earnings 30 days out -> clear
    assert earnings_blackout(today + timedelta(days=30), today, 14, 7) is False
    # exactly on the threshold (21) -> blackout (conservative)
    assert earnings_blackout(today + timedelta(days=21), today, 14, 7) is True
    # unknown earnings -> never blocks
    assert earnings_blackout(None, today, 14, 7) is False


def test_build_provider_failopen():
    assert isinstance(build_earnings_provider(Settings(entry={"earnings_gate": False})),
                      NullEarningsProvider)
    # gate on but no broker + no token (and/or no mcp extra) -> still a no-op, never raises
    assert isinstance(build_earnings_provider(Settings(entry={"earnings_gate": True})),
                      NullEarningsProvider)


# --- BrokerEarningsProvider (reuse the broker's OAuth session) -----------------------------------

class _FakeBroker:
    """Minimal stand-in for RobinhoodMCPBroker: a probed tool list + a _call_tool that returns a
    canned payload (dict or JSON text) or raises."""
    def __init__(self, tools, *, payload=None, raise_exc=None, connected=True):
        self._connected = connected
        self._tools = list(tools)
        self._payload = payload
        self._raise = raise_exc
        self.calls: list[tuple[str, dict]] = []

    async def _call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self._raise is not None:
            raise self._raise
        return self._payload


@pytest.mark.asyncio
async def test_broker_provider_reads_and_caches():
    broker = _FakeBroker(["get_option_positions", "get_earnings_results"],
                         payload={"data": {"results": BULL_RESULTS}})
    prov = BrokerEarningsProvider(broker)
    assert prov._tool == "get_earnings_results"
    expected = parse_next_earnings(BULL_RESULTS, utcnow().date())
    assert await prov.next_earnings("bull") == expected
    assert broker.calls == [("get_earnings_results", {"symbol": "BULL"})]
    await prov.next_earnings("BULL")  # cached -> no second call
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_broker_provider_parses_text_payload():
    broker = _FakeBroker(["get_earnings_results"],
                         payload=json.dumps({"data": {"results": BULL_RESULTS}}))
    prov = BrokerEarningsProvider(broker)
    assert await prov.next_earnings("BULL") == parse_next_earnings(BULL_RESULTS, utcnow().date())


@pytest.mark.asyncio
async def test_broker_provider_failopen_on_error():
    broker = _FakeBroker(["get_earnings_results"], raise_exc=RuntimeError("boom"))
    assert await BrokerEarningsProvider(broker).next_earnings("BULL") is None


@pytest.mark.asyncio
async def test_broker_provider_no_earnings_tool():
    prov = BrokerEarningsProvider(_FakeBroker(["get_option_positions"]))
    assert prov._tool is None
    assert await prov.next_earnings("BULL") is None


def test_build_prefers_connected_broker():
    broker = _FakeBroker(["get_earnings_results", "get_option_chains"])
    prov = build_earnings_provider(Settings(entry={"earnings_gate": True}), broker)
    assert isinstance(prov, BrokerEarningsProvider)
    assert prov._tool == "get_earnings_results"


def test_build_skips_broker_without_earnings_tool():
    # connected but no earnings tool, and no ROBINHOOD_MCP_TOKEN -> fail open
    broker = _FakeBroker(["get_option_chains"])
    assert isinstance(build_earnings_provider(Settings(entry={"earnings_gate": True}), broker),
                      NullEarningsProvider)


def test_build_skips_unconnected_broker():
    broker = _FakeBroker(["get_earnings_results"], connected=False)
    assert isinstance(build_earnings_provider(Settings(entry={"earnings_gate": True}), broker),
                      NullEarningsProvider)


# --- scanner integration ------------------------------------------------------------------------

class _FakeEarnings:
    def __init__(self, days):
        self._days = days

    async def next_earnings(self, symbol):
        return utcnow().date() + timedelta(days=self._days)


def _scanner(tmp_path, earnings):
    db = Database(tmp_path / "e.db")
    settings = Settings(broker="paper", market_data="paper")
    settings.entry.enabled = True
    settings.entry.watchlist = ["ONDS"]
    settings.entry.criteria.dte_max = 14
    settings.entry.criteria.exclude_earnings_days = 7
    audit = AuditStore(db)
    return OpportunityScanner(
        settings, PaperBroker(seed_positions=[]), PaperMarketData(),
        EntryDecisionStore(db), executor=None, audit=audit,
        killswitch=KillSwitch(db, audit), earnings=earnings,
    )


@pytest.mark.asyncio
async def test_scanner_skips_when_earnings_near(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    sc = _scanner(tmp_path, _FakeEarnings(days=5))  # within 14+7
    await sc.run_once()
    assert any(s["symbol"] == "ONDS" and "earnings" in s["reason"] for s in sc.last_skips)
    assert sc.last_context["ONDS"].days_to_earnings == 5


@pytest.mark.asyncio
async def test_scanner_allows_when_earnings_far(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    sc = _scanner(tmp_path, _FakeEarnings(days=60))
    await sc.run_once()
    assert not any(s["symbol"] == "ONDS" for s in sc.last_skips)  # earnings far -> not gated
    assert sc.last_context["ONDS"].days_to_earnings == 60
