"""Technical setup detection: indicator helpers, each pattern family, partial-bar handling, the
opt-in gates, the ranking tilt, the analytics bucket, and the brief rendering. Pure/synthetic."""
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from agentic.config import EntryConfig, EntryCriteria, SetupConfig
from agentic.entry import indicators as ind
from agentic.entry.context import UnderlyingContext, passes_underlying_gates
from agentic.entry.setups import (
    AVOID, FAVORABLE, active_setups, compose, detect_flags, detect_setups, setup_sort_key,
)
from agentic.marketdata.econ_calendar import EconEvent
from agentic.services.analytics import build_feature_analytics
from agentic.services.setups_view import build_setups_view
from agentic.services.weekly_brief import build_weekly_brief

CFG = SetupConfig()
CRIT = EntryCriteria()


def _bars(closes, highs=None, lows=None, vols=None, opens=None):
    n = len(closes)
    highs = highs or [c + 0.5 for c in closes]
    lows = lows or [c - 0.5 for c in closes]
    vols = vols if vols is not None else [1000] * n
    opens = opens or list(closes)
    return [{"o": opens[i], "h": highs[i], "l": lows[i], "c": closes[i], "v": vols[i]}
            for i in range(n)]


# --- indicator helpers ---------------------------------------------------------------------------

def test_bollinger_family_on_constant_series():
    flat = [100.0] * 40
    assert ind.bollinger(flat, 20) == (100.0, 100.0, 100.0)
    assert ind.bb_percent_b(flat, 20) is None          # zero width -> undefined, not 0/100
    assert ind.bb_width_pct(flat, 20) == 0.0
    assert len(ind.bb_width_series([100 + (i % 3) for i in range(100)], 20, lookback=60)) == 61


def test_keltner_and_ema_basic():
    closes = [100 + (i % 5) for i in range(40)]
    highs, lows = [c + 1 for c in closes], [c - 1 for c in closes]
    kc = ind.keltner(highs, lows, closes, 20, 1.5)
    assert kc is not None and kc[0] < kc[1] < kc[2]
    assert ind.ema(closes, 20) is not None and ind.ema(closes, 50) is None


def test_donchian_excludes_the_tested_bar():
    highs = [1.0] * 20 + [100.0]
    lows = [0.5] * 21
    assert ind.donchian(highs, lows, 20, exclude_last=True) == (0.5, 1.0)     # last bar left out
    assert ind.donchian(highs, lows, 20, exclude_last=False)[1] == 100.0


def test_volume_ratio_fail_open_on_missing_volume():
    assert ind.volume_ratio([100] * 20 + [300], 20) == 3.0
    assert ind.volume_ratio([100] * 19 + [0] + [300], 20) is None   # a 0 in the window (RH)
    assert ind.volume_ratio([100] * 20 + [0], 20) is None            # last bar has no volume
    assert ind.volume_ratio([100] * 5, 20) is None                   # too short


# --- pattern families ----------------------------------------------------------------------------

def _washout_bars(vols=None):
    closes = [100.0] * 80 + [96, 92, 88, 84, 80, 76, 72, 68]
    return _bars(closes, vols=vols)


def test_washout_flags_and_label():
    flags, feat = detect_flags(_washout_bars(), CFG)
    assert flags["oversold"] is True and flags["below_lower_bb"] is True
    assert flags["stretched_below_ma"] is True
    assert "washout" in compose(flags)
    assert feat["rsi"] is not None and feat["rsi"] <= 30


def test_washout_at_support_and_falling_knife_priority():
    read = detect_setups(_washout_bars(), CFG, support=68.0)
    assert read is not None
    assert "washout_at_support" in read.fired_now
    assert "falling_knife" in read.fired_now                 # broke the Donchian low while washing out
    assert read.primary == "breakdown" and read.primary in AVOID   # AVOID beats FAVORABLE; range break ranks first
    assert read.bias == "mixed"


def test_squeeze_coiling_near_resistance():
    noisy = [100 + (3 if i % 2 == 0 else -3) for i in range(40)]
    tight = [100 + (0.05 if i % 2 == 0 else -0.05) for i in range(60)]
    bars = _bars(noisy + tight)
    flags, _ = detect_flags(bars, CFG)
    assert flags["bb_squeeze"] is True and flags["ttm_squeeze"] is True
    read = detect_setups(bars, CFG, resistance=bars[-1]["c"] * 1.02)   # 2% overhead
    assert "coiling" in read.fired_now and "coiling_near_resistance" in read.fired_now
    # the tight, quiet range is ALSO a quiet_base (favorable); coiling itself is neutral
    from agentic.entry.setups import label_bias
    assert "quiet_base" in read.fired_now and read.bias == "favorable"
    assert label_bias("coiling") == "neutral" and label_bias("coiling_near_resistance") == "neutral"


def _range_bars(last_close, last_vol, base_vol=1000):
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [last_close]
    vols = [base_vol] * 70 + [last_vol]
    return _bars(closes, vols=vols)


def test_breakout_confirmed_on_volume_and_plain_without():
    read = detect_setups(_range_bars(12.0, 3000), CFG)
    assert "breakout_confirmed" in read.fired_now
    # breakouts are AVOID for a put seller: the primary read is an avoid label with a negative score
    # (a quiet_base still active from 2 bars back makes the overall bias "mixed", which is honest)
    assert read.primary in AVOID and read.score < 0 and read.bias in ("avoid", "mixed")
    read0 = detect_setups(_range_bars(12.0, 0, base_vol=0), CFG)      # no volume data at all
    assert "breakout" in read0.fired_now and "breakout_confirmed" not in read0.fired_now
    assert read0.flags["volume_breakout"] is None                       # unknown, not False


def test_breakdown_confirmed_is_avoid():
    read = detect_setups(_range_bars(8.5, 3000), CFG)
    assert "breakdown_confirmed" in read.fired_now
    # the tight synthetic range also reads as a squeeze ("coiling"), so the bias is mixed --
    # what matters: the confirmed breakdown is PRIMARY and the score is negative.
    assert read.bias in ("avoid", "mixed")
    assert read.primary == "breakdown_confirmed" and read.score < 0


def test_support_test_rejection_and_volume():
    closes = [100 + (0.2 if i % 2 == 0 else -0.2) for i in range(70)] + [100.3]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.5 for c in closes]
    opens = list(closes)
    highs[-1], lows[-1], opens[-1] = 100.5, 98.5, 100.0     # pierced 99 support, closed in top 40%
    read = detect_setups(_bars(closes, highs, lows, [1000] * 70 + [2500], opens), CFG, support=99.0)
    assert "support_test_rejection" in read.fired_now
    assert "support_test_on_volume" in read.fired_now         # 2.5x volume anomaly
    assert read.flags["near_support"] is True and read.flags["rejection_candle"] is True


def test_partial_bar_reads_live_but_never_confirms():
    bars = _range_bars(12.0, 3000)                             # last bar = the breakout bar
    read = detect_setups(bars, CFG, last_bar_partial=True)     # ...but it's still forming
    assert read.live["breakout_attempt"] is True
    assert "breakout" not in read.setups and "breakout_confirmed" not in read.setups


def test_active_setups_window():
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [12.0, 12.1, 12.05]
    bars = _bars(closes)
    assert "breakout" in detect_setups(bars, CFG).setups                       # fired 2 bars ago, active_bars=3
    assert "breakout" not in detect_setups(bars, CFG).fired_now
    assert "breakout" not in detect_setups(bars, SetupConfig(active_bars=1)).setups
    assert active_setups(bars, CFG)["breakout"] == 2


def test_too_little_history_returns_none():
    assert detect_setups(_bars([10.0] * 30), CFG) is None


# --- gates + ranking -----------------------------------------------------------------------------

def test_setup_gates_block_pass_and_fail_open():
    avoid = CRIT.model_copy(update={"avoid_setups": ["breakdown_confirmed"]})
    require = CRIT.model_copy(update={"require_setups": ["washout_at_support"]})
    bad = UnderlyingContext(symbol="X", setups=["breakdown_confirmed", "breakdown"])
    assert "avoid_setups" in passes_underlying_gates(bad, avoid)
    assert passes_underlying_gates(UnderlyingContext(symbol="X", setups=["coiling"]), avoid) is None
    assert "no required setup" in passes_underlying_gates(
        UnderlyingContext(symbol="X", setups=["coiling"]), require)
    assert passes_underlying_gates(UnderlyingContext(symbol="X", setups=["washout_at_support"]), require) is None
    nodata = UnderlyingContext(symbol="X")                     # no read -> both gates skip
    assert passes_underlying_gates(nodata, avoid) is None and passes_underlying_gates(nodata, require) is None


def test_setup_sort_key_orders_favorable_neutral_avoid():
    ctxs = {"F": SimpleNamespace(setup_score=2), "N": SimpleNamespace(setup_score=None),
            "A": SimpleNamespace(setup_score=-2)}
    cands = [SimpleNamespace(underlying=u, theta_efficiency=t)
             for u, t in (("A", 0.9), ("N", 0.5), ("F", 0.1), ("N2", 0.7))]
    ctxs["N2"] = SimpleNamespace(setup_score=0)
    inner = lambda c: (float(c.theta_efficiency),)  # noqa: E731
    ranked = sorted(cands, key=lambda c: setup_sort_key(c, ctxs, inner), reverse=True)
    assert [c.underlying for c in ranked] == ["F", "N2", "N", "A"]   # score, then theta breaks ties


# --- learning + surfacing ------------------------------------------------------------------------

def test_analytics_buckets_primary_setup():
    rows = [{"realized_pnl": 10.0, "context": {"primary_setup": "washout", "setup_bias": "favorable"}},
            {"realized_pnl": -5.0, "context": {"primary_setup": "washout", "setup_bias": "favorable"}},
            {"realized_pnl": 3.0, "context": {}}]
    out = build_feature_analytics(rows)
    buckets = {b["bucket"]: b for b in out["by_feature"]["primary_setup"]}
    assert buckets["washout"]["n"] == 2 and buckets["none"]["n"] == 1


def test_setups_view_shape_and_gate_flag():
    read = {"flags": {"oversold": True}, "features": {"rsi": 27.0, "support_ref": 8.44},
            "setups": ["washout_at_support"], "fired_now": ["washout_at_support"],
            "live": {"breakout_attempt": False}, "bias": "favorable", "score": 1,
            "primary": "washout_at_support", "partial_bar": True}
    view = build_setups_view(
        {"BULL": read, "AAL": {"setups": [], "bias": "none", "score": 0}},
        {"BULL": SimpleNamespace(price=8.5)},
        [{"symbol": "AAL", "reason": "setup breakdown active (avoid_setups)"}],
        EntryConfig(prefer_setups=True), datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert view["enabled"] is True and view["config"]["prefer_setups"] is True
    assert view["counts"]["favorable"] == 1 and view["counts"]["none"] == 1
    by = {s["symbol"]: s for s in view["symbols"]}
    assert by["BULL"]["price"] == 8.5 and by["BULL"]["primary"] == "washout_at_support"
    assert by["AAL"]["gate"]["blocked"] is True and "avoid_setups" in by["AAL"]["gate"]["reason"]
    assert view["symbols"][0]["symbol"] == "BULL"                # sorted by score desc


def test_brief_renders_setup_line_and_section():
    now = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    ctx = {"price": 8.5, "rsi": 27.0, "setups": ["washout_at_support", "washout"],
           "setup_bias": "favorable", "dist_to_support_pct": 0.7, "support_ref": 8.44,
           "vol_ratio_20": 2.8, "live_breakout_attempt": False}
    bad = {"price": 9.0, "setups": ["breakdown_confirmed"], "setup_bias": "avoid"}
    _t, md = build_weekly_brief(
        watchlist=["BULL", "AAL"], contexts={"BULL": ctx, "AAL": bad}, candidates=[],
        tv_by_symbol={}, regime=None, news_by_symbol={}, accounts=[],
        econ_events=[EconEvent(date(2026, 9, 16), "FOMC", "high")], now=now)
    assert "## Setups firing" in md
    assert "- BULL: washout_at_support, washout" in md and "Caution (avoid selling puts into)" in md
    assert "**Setup:** washout_at_support, washout (RSI 27; +0.7% vs support 8.44; vol 2.8x)" in md
    assert "**Setup:** AVOID: breakdown_confirmed" in md


def test_config_defaults_and_per_ticker_override():
    assert SetupConfig().enabled is True and SetupConfig().active_bars == 3
    cfg = EntryConfig(per_ticker={"BULL": {"avoid_setups": ["breakdown"]}})
    assert cfg.criteria_for("bull", cfg.criteria).avoid_setups == ["breakdown"]
    assert cfg.criteria_for("F", cfg.criteria).avoid_setups is None
    assert set(FAVORABLE).isdisjoint(AVOID)


# --- candidate labels under validation (BULL post-mortem hypotheses) ------------------------------

def test_candidate_strong_from_base_followthrough_and_climax():
    from agentic.entry.setups import CANDIDATE, bars_since_range_break
    assert set(CANDIDATE).isdisjoint(FAVORABLE) and set(CANDIDATE).isdisjoint(AVOID)   # neutral by design
    base = [10.0 if i % 2 == 0 else 11.0 for i in range(70)]
    # a strong-volume break out of a long, tight base -> strong + from_base, NOT climax
    read = detect_setups(_bars(base + [12.0], vols=[1000] * 70 + [3500]), CFG)
    assert "breakout_strong" in read.fired_now and "breakout_from_base" in read.fired_now
    assert "climax_breakout" not in read.fired_now
    assert read.features["bars_since_range_break"] >= 15 and read.features["last_break_dir"] is None
    # the very next bar also breaks -> follow-through (and not a climax: that needs 2+ bars of gap)
    read2 = detect_setups(_bars(base + [12.0, 13.0], vols=[1000] * 70 + [3500, 1200]), CFG)
    assert "breakout_followthrough" in read2.fired_now and "climax_breakout" not in read2.fired_now
    # a fresh break 6 bars after the prior up-break, with no base -> climax, not from_base
    drift = [12.1, 12.0, 12.2, 12.1, 12.3, 12.2]
    read3 = detect_setups(_bars(base + [12.0] + drift + [13.5]), CFG)
    assert "climax_breakout" in read3.fired_now and "breakout_from_base" not in read3.fired_now
    assert read3.features["bars_since_range_break"] == 6 and read3.features["last_break_dir"] == "up"
    # the helper itself: no break found -> counts the windows it could test, capped at max_look
    b = _bars(base)
    hs, ls, cs = [x["h"] for x in b], [x["l"] for x in b], [x["c"] for x in b]
    assert bars_since_range_break(hs, ls, cs, 20, 60) == (50, None)     # 70 bars -> 50 windows, history ran out
    b90 = _bars([10.0 if i % 2 == 0 else 11.0 for i in range(90)])
    hs, ls, cs = [x["h"] for x in b90], [x["l"] for x in b90], [x["c"] for x in b90]
    assert bars_since_range_break(hs, ls, cs, 20, 60) == (60, None)     # enough history -> capped


def test_quiet_base_label_fires_on_slow_grind_near_lows():
    decline = [100 - i * (20 / 60) for i in range(60)]                  # 100 -> 80 over 60 bars
    base = [80 + (0.5 if i % 2 == 0 else -0.5) for i in range(15)]       # 15 bars in a ~2.5% range
    read = detect_setups(_bars(decline + base, vols=[1500] * 60 + [600] * 15), CFG)
    assert "quiet_base" in read.fired_now and read.flags["quiet_base"] is True
    assert read.features["base_range_pct"] < 12
    # an uptrend (above the 50-day) is never a quiet base
    up = [50 + i * 0.3 for i in range(75)]
    assert "quiet_base" not in detect_setups(_bars(up), CFG).fired_now


def test_put_seller_classification_and_label_bias():
    from agentic.entry.setups import ALL_LABELS, NEUTRAL, PUT_SELLER_AVOID_PRESET, label_bias
    assert set(FAVORABLE).isdisjoint(AVOID) and set(NEUTRAL).isdisjoint(FAVORABLE) and set(NEUTRAL).isdisjoint(AVOID)
    assert label_bias("quiet_base") == "favorable" and label_bias("breakout_confirmed") == "avoid"
    assert label_bias("coiling") == "neutral" and label_bias("nonsense") == "unknown"
    assert set(PUT_SELLER_AVOID_PRESET) <= set(AVOID) and "breakout_from_base" in PUT_SELLER_AVOID_PRESET
    assert set(PUT_SELLER_AVOID_PRESET) <= set(ALL_LABELS)
