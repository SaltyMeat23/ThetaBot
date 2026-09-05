"""On-demand screener: screen_universe ranking + /api/screen endpoint."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agentic.config import EntryCriteria, Settings
from agentic.domain.models import utcnow
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.screening import opportunity_scan, screen_universe
from agentic.services.killswitch import KillSwitch
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app

PERMISSIVE = EntryCriteria(delta_min=0.15, delta_max=0.35, dte_min=5, dte_max=20,
                           min_annualized_yield=0.10, min_open_interest=100, min_volume=10,
                           max_spread_pct=0.15)


def _put(sym, strike, dte, delta, bid, ask, theta=None, oi=500, vol=50):
    exp = utcnow().date() + timedelta(days=dte)
    return OptionContractQuote(
        occ_symbol=f"{sym}{strike}", underlying=sym, option_id=None, option_type="put",
        strike=strike, expiration=exp, bid=bid, ask=ask, mark=round((bid + ask) / 2, 4),
        delta=delta, iv=0.5, theta=theta, open_interest=oi, volume=vol)


class StubMD:
    def __init__(self, chains):
        self._c = chains

    async def get_chain(self, underlying):
        return self._c.get(underlying.upper(), [])


@pytest.mark.asyncio
async def test_screen_universe_ranks_by_theta_efficiency():
    md = StubMD({
        "AAA": [_put("AAA", 10, 10, -0.25, 0.20, 0.22, theta=-0.02)],   # eff 0.002
        "BBB": [_put("BBB", 10, 10, -0.25, 0.20, 0.22, theta=-0.05)],   # eff 0.005 -> first
        "CCC": [_put("CCC", 10, 10, -0.50, 0.20, 0.22, theta=-0.09)],   # delta out of band -> dropped
    })
    out = await screen_universe(md, ["AAA", "BBB", "CCC"], PERMISSIVE)
    assert [c.underlying for c in out] == ["BBB", "AAA"]  # CCC filtered; BBB (higher theta) first


@pytest.mark.asyncio
async def test_screen_universe_skips_unfetchable_symbol():
    class Flaky(StubMD):
        async def get_chain(self, underlying):
            if underlying == "BAD":
                raise RuntimeError("chain fetch failed")
            return self._c.get(underlying, [])
    md = Flaky({"GOOD": [_put("GOOD", 10, 10, -0.25, 0.20, 0.22, theta=-0.03)]})
    out = await screen_universe(md, ["BAD", "GOOD"], PERMISSIVE)
    assert len(out) == 1 and out[0].underlying == "GOOD"


class StubUniverse(StubMD):
    def __init__(self, chains, actives, prices):
        super().__init__(chains)
        self._act, self._pr = actives, prices

    async def most_active_symbols(self, top=100):
        return self._act[:top]

    async def snapshot_prices(self, symbols):
        return {s: self._pr[s] for s in symbols if s in self._pr}


def _universe_md():
    return StubUniverse(
        chains={"AAA": [_put("AAA", 10, 10, -0.25, 0.20, 0.22, theta=-0.05)],   # eff 0.005
                "BBB": [_put("BBB", 10, 10, -0.25, 0.20, 0.22, theta=-0.02)]},  # eff 0.002
        actives=["AAA", "BBB", "ZZZ"],
        prices={"AAA": 8.0, "BBB": 15.0, "ZZZ": 100.0})   # ZZZ out of the $7-20 band


@pytest.mark.asyncio
async def test_opportunity_scan_price_band_and_rank():
    res = await opportunity_scan(_universe_md(), PERMISSIVE, price_min=7, price_max=20)
    assert res["universe"] == 3
    assert set(res["scanned"]) == {"AAA", "BBB"}                 # ZZZ ($100) filtered out
    assert [c.underlying for c in res["candidates"]] == ["AAA", "BBB"]  # higher theta first
    assert res["prices"]["AAA"] == 8.0


@pytest.mark.asyncio
async def test_opportunity_scan_excludes_leveraged_etfs():
    md = StubUniverse(
        chains={"ONDS": [_put("ONDS", 10, 10, -0.25, 0.20, 0.22, theta=-0.05)]},
        actives=["TSLL", "MSTU", "ONDS"],                  # TSLL/MSTU are leveraged ETFs
        prices={"TSLL": 10.0, "MSTU": 9.0, "ONDS": 9.0})   # all in the $5-15 band
    res = await opportunity_scan(md, PERMISSIVE, price_min=5, price_max=15)
    assert "ONDS" in res["scanned"]                         # real underlying kept
    assert "TSLL" not in res["scanned"] and "MSTU" not in res["scanned"]   # leveraged ETFs dropped


@pytest.mark.asyncio
async def test_opportunity_scan_noop_without_universe_provider():
    # A provider lacking most_active_symbols (e.g. paper) yields an empty, non-crashing result.
    res = await opportunity_scan(StubMD({}), PERMISSIVE, price_min=7, price_max=20)
    assert res == {"universe": 0, "scanned": [], "prices": {}, "candidates": []}


def test_opportunities_endpoint():
    db = Database(":memory:")
    audit = AuditStore(db)
    deps = WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db), killswitch=KillSwitch(db, audit),
        approval_gate=None, audit=audit, positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), scanner=SimpleNamespace(market_data=_universe_md()),
    )
    client = TestClient(create_app(deps))
    r = client.post("/api/opportunities", json={"price_min": 7, "price_max": 20, "dte_min": 5,
                                                "dte_max": 20, "delta_min": 0.15, "delta_max": 0.35}).json()
    assert r["ok"] and r["count"] == 2 and r["universe"] == 3
    assert r["candidates"][0]["underlying"] == "AAA" and r["candidates"][0]["price"] == 8.0


def test_screen_endpoint():
    db = Database(":memory:")
    audit = AuditStore(db)
    scanner = SimpleNamespace(market_data=StubMD({
        "AAA": [_put("AAA", 10, 10, -0.25, 0.20, 0.22, theta=-0.02)],
        "BBB": [_put("BBB", 10, 10, -0.25, 0.20, 0.22, theta=-0.05)],
    }))
    deps = WebDeps(
        settings=Settings(mode="paper"), signals=SignalStore(db), killswitch=KillSwitch(db, audit),
        approval_gate=None, audit=audit, positions=PositionStore(db), orders=OrderStore(db),
        decisions=DecisionStore(db), scanner=scanner,
    )
    client = TestClient(create_app(deps))
    # Pass DTE overrides so the 10-DTE test contracts pass (config default is 30-45).
    r = client.post("/api/screen", json={"symbols": ["AAA", "BBB"], "dte_min": 5, "dte_max": 20,
                                         "delta_min": 0.15, "delta_max": 0.35}).json()
    assert r["ok"] and r["count"] == 2
    assert r["candidates"][0]["underlying"] == "BBB"  # ranked by theta-efficiency
    assert "break_even" in r["candidates"][0] and "theta" in r["candidates"][0]
