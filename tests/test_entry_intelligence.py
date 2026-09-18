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


# --- technical setup detection (entry/setups.py) integration -----------------------------------

@pytest.mark.asyncio
async def test_scanner_overlays_setups_into_context_and_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]          # uptrend -> enters, journals context
    sc, journal, *_ = _scanner(tmp_path, closes, CRIT)
    await sc.run_once()
    read = sc.last_setups["X"]                           # cached for /api/setups
    assert set(read) >= {"flags", "features", "setups", "fired_now", "live", "bias", "score",
                         "primary", "partial_bar"}
    assert read["partial_bar"] is True                   # market open -> last bar treated as partial
    row = journal.recent()[0]
    assert isinstance(row.context.get("setups"), list)   # journaled automatically via as_dict()
    assert row.context.get("setup_bias") in ("favorable", "avoid", "mixed", "none")
    assert row.context.get("bb_percent_b") is not None   # bot-computed %B lands without TV


@pytest.mark.asyncio
async def test_scanner_avoid_setups_gate_and_setups_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    # ~200 flat bars then a sharp break below the 20-day range: a fresh breakdown / support break.
    closes = [50 + (0.2 if i % 2 == 0 else -0.2) for i in range(200)] + [46.0, 44.0]
    crit = CRIT.model_copy(update={"avoid_setups": ["breakdown", "breakdown_confirmed", "support_break"]})
    (sc, journal, settings, killswitch, positions, orders, decisions,
     entry_decisions, audit, signals) = _scanner(tmp_path, closes, crit)
    submitted = await sc.run_once()
    assert submitted == 0 and journal.recent() == []
    assert any("avoid_setups" in s["reason"] for s in sc.last_skips)
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch, approval_gate=None,
                   audit=audit, positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions, scanner=sc, trade_journal=journal)
    d = TestClient(create_app(deps)).get("/api/setups").json()
    assert d["enabled"] is True
    x = d["symbols"][0]
    assert x["symbol"] == "X" and x["gate"]["blocked"] is True
    assert x["primary"] in ("support_break", "breakdown") and x["bias"] in ("avoid", "mixed")
    assert x["live"]["breakdown_attempt"] is True        # today's partial bar is still breaking down


@pytest.mark.asyncio
async def test_scanner_records_setup_fires_once_per_bar_and_serves_accuracy(tmp_path, monkeypatch):
    from agentic.store.db import Database as _DB
    from agentic.store.setup_events import SetupEventStore
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    # range, then a completed breakout bar, then today's partial bar: "breakout" fires on the completed bar.
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [12.0, 12.1]
    (sc, journal, settings, killswitch, positions, orders, decisions,
     entry_decisions, audit, signals) = _scanner(tmp_path, closes, CRIT)
    sc.setup_events = SetupEventStore(_DB(tmp_path / "se.db"))
    await sc.run_once()
    labels = {r["label"] for r in sc.setup_events.recent()}
    assert "breakout" in labels                                   # recorded from the completed bar
    n_first = len(sc.setup_events.recent())
    await sc.run_once()                                           # same bar again -> no duplicates
    assert len(sc.setup_events.recent()) == n_first
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch, approval_gate=None,
                   audit=audit, positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions, scanner=sc, trade_journal=journal)
    d = TestClient(create_app(deps)).get("/api/setups/accuracy").json()
    assert d["available"] is True and d["recent"] and d["recent"][0]["symbol"] == "X"
    assert all(r["n"] == 0 for r in d["rows"])                    # nothing resolved yet (no forward bars)


@pytest.mark.asyncio
async def test_scanner_merges_fresh_tv_setup_flags(tmp_path, monkeypatch):
    import time as _time
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    # The bot reads a plain "breakout" (constant volume); a fresh TradingView daily flag with a 2.2x
    # volume ratio upgrades it to breakout_confirmed, and an intraday attempt flag lights `live`.
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [12.0, 12.1]
    now_ms = int(_time.time() * 1000)
    seed = {"adx": 30.0, "breakout": 1, "vol_ratio_20": 2.2, "d_bar_time": now_ms - 3600_000,
            "i_tf": 30, "i_bar_time": now_ms - 600_000, "i_breakout_attempt": 1}
    (sc, journal, settings, killswitch, positions, orders, decisions,
     entry_decisions, audit, signals) = _scanner(tmp_path, closes, CRIT, tv_seed=seed)
    await sc.run_once()
    ctx = sc.last_context["X"]
    assert "breakout_confirmed" in ctx.setups and ctx.live_breakout_attempt is True
    assert ctx.tv_setups["breakout"] is True and "tv_daily:breakout_confirmed" in ctx.setup_sources
    row = journal.recent()[0]
    assert "breakout_confirmed" in row.context["setups"]           # the merged read is what gets journaled
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch, approval_gate=None,
                   audit=audit, positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions, scanner=sc, trade_journal=journal)
    x = TestClient(create_app(deps)).get("/api/setups").json()["symbols"][0]
    assert x["tv"]["present"] is True and x["tv"]["flags"]["breakout"] is True
    assert x["live"]["breakout_attempt"] is True


@pytest.mark.asyncio
async def test_scanner_profiles_each_name_daily_and_serves_risk_profile(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]
    (sc, journal, settings, killswitch, positions, orders, decisions,
     entry_decisions, audit, signals) = _scanner(tmp_path, closes, CRIT)
    await sc.run_once()
    prof = sc.last_risk_profile["X"]                        # profiled on its first scan
    assert prof["symbol"] == "X" and prof["n"] > 0 and "suggested_cushion" in prof
    stamp = prof["date"]
    await sc.run_once()
    assert sc.last_risk_profile["X"]["date"] == stamp        # cached for the day, not recomputed
    deps = WebDeps(settings=settings, signals=signals, killswitch=killswitch, approval_gate=None,
                   audit=audit, positions=positions, orders=orders, decisions=decisions,
                   entry_decisions=entry_decisions, scanner=sc, trade_journal=journal)
    d = TestClient(create_app(deps)).get("/api/risk-profile").json()
    assert d["profiles"][0]["symbol"] == "X" and "put_seller_avoid_preset" in d["config"]
    assert isinstance(d["proposals"], list)


@pytest.mark.asyncio
async def test_confirmed_downtrend_skip_pauses_new_puts_only_when_enabled(tmp_path, monkeypatch):
    """SPY (the stub serves the same downtrend bars for every symbol) has closed below its 200-day
    for many sessions: with the knob ON every approved put is vetoed with a market-wide skip; with
    it OFF (default) nothing changes."""
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [100.0] * 252 + [90.0 - i * 0.5 for i in range(8)]      # 8 closes under the 200-day
    for enabled in (False, True):
        sc, journal, settings, *_ = _scanner(tmp_path / str(enabled), closes, CRIT)
        settings.macro.skip_confirmed_downtrend = enabled

        async def any_bars(underlying, lookback_days=260):       # the stub only serves "X"; SPY too
            return _bars(closes)
        sc.market_data.get_underlying_bars = any_bars
        await sc.run_once()
        reg = sc.last_regime
        assert reg is not None and reg.confirmed_downtrend is True and reg.spy_days_below_sma200 == 8
        market_skips = [s for s in sc.last_skips if s.get("symbol") == "*"]
        if enabled:
            assert market_skips and "confirmed downtrend" in market_skips[0]["reason"]
            assert not journal.recent()                              # no entry journaled
        else:
            assert not market_skips
