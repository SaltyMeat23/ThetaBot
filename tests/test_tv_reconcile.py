"""Feed reconciliation: chart values vs the bot's stored TV-indicator snapshot.

Fixtures mirror the real /api/tv-indicators shape ({symbol, received_at, age_seconds, payload}).
Pure — no MCP, no network.
"""
from agentic.tools.tv_reconcile import (
    EXPECTED_KEYS,
    SCANNER_CONSUMED,
    ReconcileTolerance,
    reconcile,
    reconcile_symbol,
)

MAX_AGE = 108_000


def _row(sym, payload, age=100.0, ts="2026-09-09T15:00:00+00:00"):
    return {"symbol": sym, "age_seconds": age, "received_at": ts, "payload": payload}


def _tol(stale_after=MAX_AGE):
    return ReconcileTolerance(stale_after_seconds=stale_after)


def test_clean_match_no_divergences():
    chart = {"support": 9.55, "resistance": 10.19, "adx": 22.5, "bb_percent_b": 48.1}
    bot = {"support": 9.552, "resistance": 10.188, "adx": 23.0, "bb_percent_b": 49.0}
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert r.ok
    assert r.divergences == []


def test_percent_b_alias_flagged_as_unconsumed():
    # exporter emits percent_b; the gate reads bb_percent_b -> silently never fires.
    chart = {"bb_percent_b": 48.1}
    bot = {"percent_b": 48.1}
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    kinds = {(d.field, d.kind) for d in r.divergences}
    assert ("bb_percent_b", "unconsumed_alias") in kinds
    assert not r.ok


def test_percent_symbol_alias_flagged():
    # bot stores the value under "%b" (a KNOWN_ALIASES key) — must be caught, not mislabeled missing.
    chart = {"bb_percent_b": 48.1}
    bot = {"%b": 48.1}
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert any(d.field == "bb_percent_b" and d.kind == "unconsumed_alias" for d in r.divergences)


def test_support_numeric_drift():
    chart = {"support": 9.55}
    bot = {"support": 9.20}  # ~3.7% off, beyond 0.5% price tolerance
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert any(d.field == "support" and d.kind == "numeric_drift" for d in r.divergences)


def test_support_within_tolerance_ok():
    chart = {"support": 9.55}
    bot = {"support": 9.57}  # ~0.2% off, within 0.5%
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert r.ok


def test_lingering_on_bot():
    chart = {"resistance": 10.19}          # chart no longer emits support
    bot = {"support": 7.66, "resistance": 10.19}
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert any(d.field == "support" and d.kind == "lingering_on_bot" for d in r.divergences)


def test_type_mismatch_string_value():
    chart = {"support": 9.55}
    bot = {"support": "9.55"}  # scanner's isinstance guard skips strings
    r = reconcile_symbol("F", chart, bot, 100.0, tol=_tol())
    assert any(d.field == "support" and d.kind == "type_mismatch" for d in r.divergences)


def test_missing_on_bot_when_absent():
    chart = {"support": 9.55, "adx": 22.0}
    r = reconcile_symbol("F", chart, None, None, tol=_tol())
    assert not r.present_on_bot and not r.ok
    assert {d.kind for d in r.divergences} == {"missing_on_bot"}


def test_stale_marks_not_ok():
    chart = {"support": 9.55}
    bot = {"support": 9.55}
    r = reconcile_symbol("F", chart, bot, 200_000.0, tol=_tol())  # older than 30h
    assert r.stale and not r.ok


def test_reconcile_uses_live_threshold_when_tol_unset():
    chart_by_symbol = {"F": {"support": 9.55}}
    bot_recent = [_row("F", {"support": 9.55}, age=200_000.0)]
    # tol has no stale_after_seconds; reconcile() must inject max_age_seconds
    reports = reconcile(chart_by_symbol, bot_recent, tol=ReconcileTolerance(), max_age_seconds=MAX_AGE)
    assert reports[0].stale is True


def test_contract_constants_match_scanner_keys():
    # Guard: these are the keys scanner.py actually reads. If someone renames a gate key without
    # updating this set (and the Pine emit), the coupling breaks silently — this pins it.
    assert SCANNER_CONSUMED == {"support", "adx", "bb_percent_b"}
    assert "resistance" in EXPECTED_KEYS
