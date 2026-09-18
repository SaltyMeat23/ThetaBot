"""TradingView real-time setup layer: wire-format + freshness parsing and union merge semantics."""
import time

from agentic.entry.setups import merge_tv, parse_tv_setups

NOW = int(time.time() * 1000)
H = 3600_000


def test_parse_daily_flags_freshness_and_wire_format():
    fresh = {"breakout": 1, "breakdown": 0, "squeeze_on": 1, "vol_ratio_20": 2.4, "d_bar_time": NOW - H}
    flags, age = parse_tv_setups(fresh, now_ms=NOW)
    assert flags["breakout"] is True and flags["breakdown"] is False and flags["squeeze_on"] is True
    assert flags["vol_ratio_20"] == 2.4 and 3500 < age < 3700
    stale = dict(fresh, d_bar_time=NOW - 5 * 24 * H)
    assert parse_tv_setups(stale, now_ms=NOW)[0] == {}                         # > 2 days -> ignored
    assert parse_tv_setups({"breakout": True, "d_bar_time": NOW - H}, now_ms=NOW)[0] == {}  # bool rejected
    assert parse_tv_setups({"breakout": 1}, now_ms=NOW)[0] == {}                # no bar time -> unjudgeable
    # keys from the other alerts in the merged snapshot are simply ignored
    assert parse_tv_setups({"support": 8.4, "adx": 30, "d_bar_time": NOW - H}, now_ms=NOW)[0] == {}


def test_parse_intraday_flags_use_tf_window():
    intr = {"i_tf": 30, "i_bar_time": NOW - 20 * 60_000, "i_breakout_attempt": 1,
            "i_support_test": 0, "i_vol_anomaly": 1}
    f, age = parse_tv_setups(intr, now_ms=NOW)
    assert f["i_breakout_attempt"] is True and f["i_support_test"] is False and f["i_vol_anomaly"] is True
    assert age is not None and age < 1300
    old = dict(intr, i_bar_time=NOW - 90 * 60_000)                              # > 2 x 30m -> ignored
    assert parse_tv_setups(old, now_ms=NOW)[0] == {}
    hourly = dict(intr, i_tf=60, i_bar_time=NOW - 90 * 60_000)                  # within 2 x 60m -> kept
    assert parse_tv_setups(hourly, now_ms=NOW)[0]["i_breakout_attempt"] is True


def test_merge_tv_adds_upgrades_never_removes():
    setups, live, src = merge_tv(["breakout", "coiling"], {"breakout_attempt": None},
                                 {"breakout": True, "vol_ratio_20": 2.0})
    assert "breakout_confirmed" in setups and "coiling" in setups and "breakout" in setups
    assert src == ["tv_daily:breakout_confirmed"]
    setups, live, src = merge_tv(["coiling"], {}, {"breakout": True})          # no volume on the wire
    assert "breakout" in setups and "breakout_confirmed" not in setups
    setups, live, src = merge_tv(["washout"], {}, {"i_breakout_attempt": True, "i_support_test": True})
    assert live["breakout_attempt"] is True and "support_test_rejection" in setups and "washout" in setups
    assert src == ["tv_intraday:breakout_attempt", "tv_intraday:support_test_rejection"]
    setups, live, src = merge_tv(["breakdown"], {}, {"breakout": False, "breakdown": False})
    assert setups == ["breakdown"] and src == []                                # false flags change nothing
    setups, _l, _s = merge_tv(None, None, {"breakdown": True, "vol_ratio_20": 3.0})
    assert setups[0] == "breakdown_confirmed"                                   # PRIORITY-ordered output
