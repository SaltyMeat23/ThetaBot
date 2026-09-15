"""Pre-market brief builder: renders trend/S-R/strike-room/freshness; flags stale feed. Pure."""
from agentic.services.premarket_brief import build_premarket_brief

MAX_AGE = 108_000


def _tv_health(symbols):
    return {"threshold_seconds": MAX_AGE, "symbols": symbols}


def _bot_config(per_ticker=None):
    return {"editable": {"entry": {
        "watchlist": ["F", "SOFI"],
        "criteria": {"delta_min": 0.20, "delta_max": 0.30, "require_strike_below_support": False},
        "per_ticker": per_ticker or {},
    }}}


def test_brief_has_all_sections():
    chart = {"F": {"price": 9.56, "support": 9.55, "resistance": 10.19, "adx": 26.0, "sma200": 8.9}}
    bot_indicators = [{"symbol": "F", "age_seconds": 100.0, "payload": {"support": 9.55, "resistance": 10.19}}]
    health = _tv_health([{"symbol": "F", "present": True, "stale": False, "age_seconds": 100.0}])
    title, md = build_premarket_brief(
        watchlist_name="default", symbols=["F"], chart_by_symbol=chart,
        bot_indicators=bot_indicators, tv_health=health, bot_config=_bot_config())
    assert "Pre-market brief" in title
    assert "### F" in md
    assert "Trend:" in md and "200-SMA" in md and "trending" in md
    assert "Structural S/R:" in md and "9.55" in md
    assert "Strike room:" in md and "vs support" in md
    assert "Feed:" in md and "fresh" in md
    assert "not investment advice" in md.lower()


def test_brief_flags_stale_feed():
    chart = {"F": {"price": 9.56, "support": 9.55}}
    bot_indicators = [{"symbol": "F", "age_seconds": 200_000.0, "payload": {"support": 9.55}}]
    health = _tv_health([{"symbol": "F", "present": True, "stale": True, "age_seconds": 200_000.0}])
    _title, md = build_premarket_brief(
        watchlist_name="default", symbols=["F"], chart_by_symbol=chart,
        bot_indicators=bot_indicators, tv_health=health, bot_config=_bot_config())
    assert "STALE" in md
    assert "Feed stale/missing for: F" in md


def test_brief_uses_per_ticker_override():
    chart = {"SOFI": {"price": 17.0, "support": 16.5}}
    bot_indicators = [{"symbol": "SOFI", "age_seconds": 50.0, "payload": {"support": 16.5}}]
    health = _tv_health([{"symbol": "SOFI", "present": True, "stale": False, "age_seconds": 50.0}])
    cfg = _bot_config(per_ticker={"SOFI": {"delta_max": 0.22, "require_strike_below_support": True}})
    _title, md = build_premarket_brief(
        watchlist_name="default", symbols=["SOFI"], chart_by_symbol=chart,
        bot_indicators=bot_indicators, tv_health=health, bot_config=cfg)
    assert "0.22" in md              # per-ticker delta_max reflected
    assert "support-gate ON" in md   # per-ticker override enabled the gate


def test_brief_annotates_feed_divergence():
    # chart shows support 9.55; bot stored it under a stale/aliased name -> divergence surfaced.
    chart = {"F": {"price": 9.56, "support": 9.55, "bb_percent_b": 48.0}}
    bot_indicators = [{"symbol": "F", "age_seconds": 100.0, "payload": {"support": 9.55, "percent_b": 48.0}}]
    health = _tv_health([{"symbol": "F", "present": True, "stale": False, "age_seconds": 100.0}])
    _title, md = build_premarket_brief(
        watchlist_name="default", symbols=["F"], chart_by_symbol=chart,
        bot_indicators=bot_indicators, tv_health=health, bot_config=_bot_config())
    assert "Feed divergence" in md and "bb_percent_b" in md
