"""Setup-accuracy tracker: forward returns, hit/MAE aggregation, the walk-forward replay, and the
SetupEventStore (dedup, pending, resolve). Pure/synthetic; store tests use a temp SQLite DB."""
from datetime import date, datetime, timedelta, timezone

from agentic.config import SetupConfig
from agentic.entry.setups import SetupRead
from agentic.services.setup_tracker import (
    aggregate_accuracy, forward_returns, record_fires, replay_history, resolve_for_symbol,
)
from agentic.store.db import Database
from agentic.store.setup_events import SetupEventStore

D0 = date(2026, 1, 5)


def _dated(closes, start=D0, vols=None):
    out = []
    for i, c in enumerate(closes):
        out.append({"date": (start + timedelta(days=i)).isoformat(), "o": c, "h": c + 0.5,
                    "l": c - 0.5, "c": c, "v": (vols[i] if vols else 1000)})
    return out


def test_forward_returns_and_excursions():
    closes = [100.0] * 5 + [102, 104, 101, 99, 105, 107, 103, 110, 108, 109, 111]
    bars = _dated(closes)
    fire = bars[4]                                      # date index 4, price 100
    fr = forward_returns(bars, fire["date"], fire["c"])
    assert fr["ret_5d"] == round((105 - 100) / 100, 5)   # 5 bars later = 105
    assert fr["ret_10d"] == round((109 - 100) / 100, 5)  # 10 bars later = 109
    assert fr["mae_10d"] == round((99 - 0.5 - 100) / 100, 5)     # lowest low in the window
    assert fr["mfe_10d"] == round((110 + 0.5 - 100) / 100, 5)    # highest high in the window
    assert forward_returns(bars, bars[-3]["date"], 100.0) is None   # not enough bars after
    assert forward_returns(bars, "1999-01-01", 100.0) is None       # unknown date


def test_aggregate_accuracy_hit_logic_and_pending():
    rows = [
        {"label": "washout", "source": "bot_daily", "resolved_at": "x", "ret_5d": 0.02, "ret_10d": 0.03, "mae_10d": -0.01},
        {"label": "washout", "source": "bot_daily", "resolved_at": "x", "ret_5d": -0.04, "ret_10d": -0.02, "mae_10d": -0.06},
        {"label": "washout", "source": "bot_daily", "resolved_at": None},                 # pending
        {"label": "breakdown", "source": "bot_daily", "resolved_at": "x", "ret_5d": -0.05, "ret_10d": -0.08, "mae_10d": -0.09},
    ]
    acc = {(r["label"], r["source"]): r for r in aggregate_accuracy(rows)}
    w = acc[("washout", "bot_daily")]
    assert w["n"] == 2 and w["n_pending"] == 1 and w["hit_rate_5d"] == 0.5    # favorable: >= 0 is a hit
    assert w["avg_ret_5d"] == round((0.02 - 0.04) / 2, 4) and w["avg_mae_10d"] == -0.035
    b = acc[("breakdown", "bot_daily")]
    assert b["hit_rate_5d"] == 1.0 and b["bias"] == "avoid"                    # avoid: a fall is a hit


def test_replay_history_fires_and_resolves_on_history():
    # 70 range bars, a breakout on volume, then a rally: the replay must attach the forward outcome.
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [12.0] + [12.5, 13, 13.5, 14, 14.5,
                                                                        15, 15.5, 16, 16.5, 17, 17.5]
    vols = [1000] * 70 + [3000] + [1200] * 11
    rows = replay_history({"X": _dated(closes, vols=vols)}, SetupConfig())
    bo = [r for r in rows if r["label"] == "breakout_confirmed"]
    assert bo and bo[0]["symbol"] == "X" and bo[0]["source"] == "replay"
    assert bo[0]["fire_price"] == 12.0 and bo[0]["ret_5d"] > 0 and bo[0]["ret_10d"] > 0
    assert len({(r["label"], r["bar_date"]) for r in rows}) == len(rows)       # one row per bar/label
    acc = {r["label"]: r for r in aggregate_accuracy(rows)}
    # breakout_confirmed is an AVOID label for a put seller, so a rally after it is a MISS (the
    # forward return itself is positive; the hit definition is what flips)
    assert acc["breakout_confirmed"]["n"] == 1 and acc["breakout_confirmed"]["hit_rate_5d"] == 0.0
    assert acc["breakout_confirmed"]["avg_ret_5d"] > 0 and acc["breakout_confirmed"]["bias"] == "avoid"


def _read(labels):
    return SetupRead(fired_now=list(labels), setups=list(labels), features={"rsi": 25.0,
                     "active_bars_ago": {}})


def test_store_record_dedup_pending_resolve_and_accuracy(tmp_path):
    store = SetupEventStore(Database(tmp_path / "se.db"))
    now = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
    bars = _dated([100.0] * 70 + [90.0])            # completed bar dated D0+70
    n = record_fires(store, "X", _read(["washout", "washout_at_support"]), bars, now, partial=False)
    assert n == 2
    assert record_fires(store, "X", _read(["washout"]), bars, now, partial=False) == 0   # same bar -> dedup
    pend = store.pending("X")
    assert len(pend) == 2 and pend[0]["fire_price"] == 90.0 and pend[0]["features"]["rsi"] == 25.0
    # Not enough bars after the fire yet -> nothing resolves.
    assert resolve_for_symbol(store, "X", bars, now) == 0
    # Ten more bars arrive -> both resolve with forward returns.
    later = _dated([100.0] * 70 + [90.0] + [92, 94, 93, 95, 96, 97, 98, 99, 100, 101])
    assert resolve_for_symbol(store, "X", later, now + timedelta(days=12)) == 2
    assert store.pending("X") == []
    acc = {r["label"]: r for r in store.accuracy()}
    assert acc["washout"]["n"] == 1 and acc["washout"]["hit_rate_5d"] == 1.0
    assert acc["washout"]["avg_ret_5d"] == round((96 - 90) / 90, 4)
    assert len(store.recent()) == 2


def test_record_fires_skips_partial_bar_and_uses_completed_bar(tmp_path):
    store = SetupEventStore(Database(tmp_path / "se2.db"))
    now = datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc)
    bars = _dated([100.0] * 70 + [95.0, 93.0])       # last bar is the partial one
    record_fires(store, "X", _read(["breakdown"]), bars, now, partial=True)
    ev = store.pending("X")[0]
    assert ev["fire_price"] == 95.0 and ev["bar_date"] == bars[-2]["date"]   # completed bar, not partial


def test_resolve_calendar_fallback_without_dates(tmp_path):
    """Bars without a `date` (Robinhood) can't be located by date, so a fire resolves by calendar
    after >= 14 days using the latest close (ret_10d approximated; mae/mfe left None)."""
    store = SetupEventStore(Database(tmp_path / "se3.db"))
    fired = datetime(2026, 9, 1, 20, 0, tzinfo=timezone.utc)
    at_fire = [{"o": 100, "h": 100.5, "l": 99.5, "c": 100.0, "v": 0}] * 70          # fire price = 100
    record_fires(store, "R", _read(["coiling"]), at_fire, fired, partial=False)
    later = at_fire + [{"o": 110, "h": 110.5, "l": 109.5, "c": 110.0, "v": 0}]      # latest close = 110
    assert resolve_for_symbol(store, "R", later, fired + timedelta(days=5)) == 0     # too soon
    assert resolve_for_symbol(store, "R", later, fired + timedelta(days=15)) == 1    # >= 14d -> approx
    r = store.recent()[0]
    assert r["ret_5d"] is None and r["mae_10d"] is None
    assert r["ret_10d"] == round((110 - 100) / 100, 5)


def test_episode_start_dedup_in_aggregate_and_replay():
    rows = [{"label": "coiling", "source": "replay", "resolved_at": "x", "ret_5d": 0.01, "ret_10d": 0.02,
             "mae_10d": -0.01, "episode_start": True},
            {"label": "coiling", "source": "replay", "resolved_at": "x", "ret_5d": 0.03, "ret_10d": 0.04,
             "mae_10d": -0.02, "episode_start": False}]
    a = aggregate_accuracy(rows)[0]
    assert a["n"] == 2 and a["n_ep"] == 1                           # only the run's first bar counts as an episode
    assert a["avg_ret_5d"] == 0.02 and a["ep_avg_ret_5d"] == 0.01
    # unknown flag (older rows) counts as a start rather than being dropped
    assert aggregate_accuracy([dict(rows[0], episode_start=None)])[0]["n_ep"] == 1
    # replay: a squeeze that persists for many bars opens ONE episode, then re-fires
    closes = [100 + (0.05 if i % 2 == 0 else -0.05) for i in range(100)]
    co = [r for r in replay_history({"X": _dated(closes)}, SetupConfig()) if r["label"] == "coiling"]
    assert co and co[0]["episode_start"] is True
    assert sum(1 for r in co if r["episode_start"]) < len(co)


def test_record_fires_stamps_episode_start(tmp_path):
    store = SetupEventStore(Database(tmp_path / "se4.db"))
    now = datetime(2026, 9, 15, 20, 0, tzinfo=timezone.utc)
    bars = _dated([100.0] * 70 + [90.0])
    record_fires(store, "X", _read(["washout", "coiling"]), bars, now, partial=False,
                 prev_labels={"coiling"})
    ev = {r["label"]: r for r in store.pending("X")}
    assert ev["washout"]["features"]["episode_start"] is True       # new this bar
    assert ev["coiling"]["features"]["episode_start"] is False      # continuing from the prior bar
