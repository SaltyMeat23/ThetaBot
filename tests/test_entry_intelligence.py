"""Underlying context/gates, scanner integration (journal context + skips), scan-status."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, EntryCriteria, Settings
from agentic.domain.models import utcnow
from agentic.entry.context import UnderlyingContext, build_context, passes_underlying_gates
from agentic.store.tv_indicators import TVIndicatorStore
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.trade_journal import TradeJournalStore
from agentic.web.app import WebDeps, create_app

CRIT = EntryCriteria(delta_min=0.20, delta_max=0.30, dte_min=30, dte_max=45,
                     min_annualized_yield=0.10, min_open_interest=100, min_volume=10,
                     max_spread_pct=0.15, exclude_earnings_days=0)


def _bars(closes):
    return [{"o": c, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 1000} for c in closes]


def test_build_context_uptrend():
    closes = [10 + i * 0.1 for i in range(260)]     # steady uptrend
    ctx = build_context("X", _bars(closes), atm_iv=0.5, iv_history=[], criteria=CRIT)
    assert ctx.price == closes[-1]
    assert ctx.above_sma200 is True
    assert ctx.rsi == 100.0
    assert ctx.iv_rank is None                       # not enough IV history


def test_gates_block_and_pass():
    down = [40 - i * 0.1 for i in range(260)]        # downtrend
    ctx = build_context("X", _bars(down), atm_iv=0.5, iv_history=[], criteria=CRIT)
    crit = CRIT.model_copy(update={"require_above_sma200": True})
    assert passes_underlying_gates(ctx, crit) is not None       # below 200-day -> blocked
    # Unknown data never blocks: short history -> above_sma200 None.
    short_ctx = build_context("X", _bars([10, 11, 12]), atm_iv=0.5, iv_history=[], criteria=CRIT)
    assert short_ctx.above_sma200 is None
    assert passes_underlying_gates(short_ctx, crit) is None
    # RSI floor blocks a deep-oversold name.
    rsi_ctx = build_context("X", _bars(down), atm_iv=0.5, iv_history=[], criteria=CRIT)
    assert passes_underlying_gates(rsi_ctx, CRIT.model_copy(update={"rsi_min": 40})) is not None


def test_max_pct_below_sma200_gate():
    crit = CRIT.model_copy(update={"max_pct_below_sma200": 0.10})   # allow down to 10% below
    shallow = UnderlyingContext(symbol="X", price=8.95, sma200=9.39)   # -4.7% -> shallow dip, ok
    assert passes_underlying_gates(shallow, crit) is None
    deep = UnderlyingContext(symbol="X", price=8.64, sma200=14.46)     # -40% -> broken, blocked
    assert passes_underlying_gates(deep, crit) is not None
    above = UnderlyingContext(symbol="X", price=10.0, sma200=9.0)      # above the 200-SMA -> ok
    assert passes_underlying_gates(above, crit) is None
    nodata = UnderlyingContext(symbol="X")                            # no price/sma -> fail-open
    assert passes_underlying_gates(nodata, crit) is None


def test_adx_and_bb_percent_b_gates():
    weak = UnderlyingContext(symbol="X", adx=18.0, bb_percent_b=10.0)
    assert passes_underlying_gates(weak, CRIT.model_copy(update={"min_adx": 25})) is not None
    assert passes_underlying_gates(weak, CRIT.model_copy(update={"min_bb_percent_b": 20})) is not None
    strong = UnderlyingContext(symbol="X", adx=30.0, bb_percent_b=55.0)
    assert passes_underlying_gates(
        strong, CRIT.model_copy(update={"min_adx": 25, "min_bb_percent_b": 20})) is None
    # Fail-open: gate configured but no TradingView value present -> never blocks.
    nodata = UnderlyingContext(symbol="X")
    assert passes_underlying_gates(
        nodata, CRIT.model_copy(update={"min_adx": 25, "min_bb_percent_b": 20})) is None


class StubMD(MarketDataProvider):
    def __init__(self, chain, closes):
        self._chain, self._closes = chain, closes

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return self._chain if underlying == "X" else []

    async def get_underlying_bars(self, underlying, lookback_days=260):
        return _bars(self._closes) if underlying == "X" else []

    async def get_underlying_price(self, underlying):
        return self._closes[-1]


def _scanner(tmp_path, closes, criteria, tv_seed=None):
    db = Database(tmp_path / "ei.db")
    audit, positions, decisions = AuditStore(db), PositionStore(db), DecisionStore(db)
    orders, entry_decisions, journal = OrderStore(db), EntryDecisionStore(db), TradeJournalStore(db)
    killswitch = KillSwitch(db, audit)
    tv = TVIndicatorStore(db)
    if tv_seed is not None:
        tv.upsert("X", tv_seed)
    exp = utcnow().date() + timedelta(days=35)
    put = OptionContractQuote(
        occ_symbol="X" + exp.strftime("%y%m%d") + "P00050000", underlying="X", option_id=None,
        option_type="put", strike=50.0, expiration=exp, bid=1.60, ask=1.70, mark=1.65,
        delta=-0.25, iv=0.45, open_interest=500, volume=50)
    md = StubMD([put], closes)
    settings = Settings(mode="paper", broker="paper",
                        entry=EntryConfig(enabled=True, watchlist=["X"], criteria=criteria))
    broker = PaperBroker(seed_positions=[], buying_power=100_000.0)
    ex = OrderExecutor(settings, broker, md, positions, orders, decisions, audit, killswitch,
                       entry_decisions=entry_decisions, trade_journal=journal,
                       poll_interval_seconds=0.001)
    sc = OpportunityScanner(settings, broker, md, entry_decisions, ex, audit, killswitch,
                            trade_journal=journal, tv_indicators=tv)
    return sc, journal, settings, killswitch, positions, orders, decisions, entry_decisions, audit, SignalStore(db)


@pytest.mark.asyncio
async def test_scanner_attaches_context_to_journal(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]          # uptrend -> passes any gate
    sc, journal, *_ = _scanner(tmp_path, closes, CRIT)
    await sc.run_once()
    row = journal.recent()[0]
    assert row.context.get("above_sma200") is True and row.context.get("rsi") == 100.0


@pytest.mark.asyncio
async def test_scanner_skips_downtrend_when_gated(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    down = [40 - i * 0.1 for i in range(260)]
    crit = CRIT.model_copy(update={"require_above_sma200": True})
    sc, journal, *_ = _scanner(tmp_path, down, crit)
    submitted = await sc.run_once()
    assert submitted == 0
    assert journal.recent() == []                         # nothing entered
    assert sc.last_skips and sc.last_skips[0]["symbol"] == "X"


@pytest.mark.asyncio
async def test_scanner_overlays_tv_features_into_context(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]          # uptrend -> enters, journals context
    sc, journal, *_ = _scanner(tmp_path, closes, CRIT, tv_seed={"adx": 31.5, "bb_percent_b": 62.0})
    await sc.run_once()
    row = journal.recent()[0]
    assert row.context.get("adx") == 31.5 and row.context.get("bb_percent_b") == 62.0


@pytest.mark.asyncio
async def test_scanner_adx_gate_skips_weak_trend(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]          # uptrend passes sma200...
    crit = CRIT.model_copy(update={"min_adx": 25})
    sc, journal, *_ = _scanner(tmp_path, closes, crit, tv_seed={"adx": 15.0})   # ...but weak ADX
    submitted = await sc.run_once()
    assert submitted == 0
    assert journal.recent() == []
    assert any("adx" in s["reason"] for s in sc.last_skips)


@pytest.mark.asyncio
async def test_scan_status_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]
    (sc, journal, settings, killswitch, positions, orders, decisions,
     entry_decisions, audit, signals) = _scanner(tmp_path, closes, CRIT)
    await sc.run_once()
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch, approval_gate=None,
                   audit=audit, positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions, scanner=sc, trade_journal=journal)
    client = TestClient(create_app(deps))
    st = client.get("/api/scan-status").json()
    assert st["enabled"] is True and st["watchlist"] == 1 and st["feed"] == "indicative"
    assert st["last_scan_at"] is not None and st["last_error"] is None


# --- IV-rank-aware ranking (prefer_iv_rank) ------------------------------------------------------

def test_iv_rank_sort_key_prefers_rich_premium():
    from agentic.services.scanner import iv_rank_sort_key
    from types import SimpleNamespace

    def cand(u, teff):
        return SimpleNamespace(underlying=u, theta_efficiency=teff)
    ctxs = {
        "RICH": UnderlyingContext(symbol="RICH", iv_rank=85.0),   # premium unusually rich
        "CHEAP": UnderlyingContext(symbol="CHEAP", iv_rank=12.0),  # unusually cheap
        "UNK": UnderlyingContext(symbol="UNK"),                    # iv_rank None -> neutral 50
    }
    cands = [cand("CHEAP", 0.009), cand("RICH", 0.004), cand("UNK", 0.006)]
    ranked = sorted(cands, key=lambda c: iv_rank_sort_key(c, ctxs), reverse=True)
    # RICH (85) first despite lower theta-eff; CHEAP (12) last despite highest theta-eff; UNK neutral middle.
    assert [c.underlying for c in ranked] == ["RICH", "UNK", "CHEAP"]


def test_iv_rank_sort_key_unknown_is_neutral_not_penalized():
    from agentic.services.scanner import iv_rank_sort_key
    from types import SimpleNamespace
    # Two unknown-IV-rank names fall back to theta-efficiency ordering (fail-open, no penalty).
    ctxs = {"A": UnderlyingContext(symbol="A"), "B": UnderlyingContext(symbol="B")}
    a = SimpleNamespace(underlying="A", theta_efficiency=0.003)
    b = SimpleNamespace(underlying="B", theta_efficiency=0.007)
    assert sorted([a, b], key=lambda c: iv_rank_sort_key(c, ctxs), reverse=True) == [b, a]
