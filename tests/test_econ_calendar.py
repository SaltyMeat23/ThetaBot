"""Curated econ calendar: parsing, upcoming-window filter, fail-open loading."""
from datetime import date

from agentic.marketdata.econ_calendar import (
    EconEvent,
    default_calendar_path,
    load_econ_calendar,
    parse_events,
    upcoming_events,
)


def test_parse_events_coerces_and_skips_malformed():
    raw = [
        {"date": "2026-09-16", "event": "FOMC", "importance": "high"},
        {"date": date(2026, 9, 17), "event": "CPI"},          # date object + default importance
        {"date": "not-a-date", "event": "bad"},               # skipped
        {"event": "no date"},                                 # skipped
        "not a dict",                                          # skipped
        {"date": "2026-09-18", "event": "PPI", "importance": "bogus"},  # importance -> medium
    ]
    evs = parse_events(raw)
    assert [e.event for e in evs] == ["FOMC", "CPI", "PPI"]
    assert evs[0].importance == "high" and evs[1].importance == "medium" and evs[2].importance == "medium"
    assert evs[1].date == date(2026, 9, 17)


def test_upcoming_events_windows_and_sorts():
    evs = [
        EconEvent(date(2026, 9, 8), "past", "high"),      # before today -> excluded
        EconEvent(date(2026, 9, 16), "FOMC", "high"),     # in window
        EconEvent(date(2026, 9, 10), "CPI", "high"),      # in window (earlier)
        EconEvent(date(2026, 9, 30), "far", "low"),       # beyond 7d -> excluded
    ]
    up = upcoming_events(evs, today=date(2026, 9, 9), days=7)
    assert [e.event for e in up] == ["CPI", "FOMC"]        # sorted by date, window-filtered


def test_load_fail_open_on_missing_file(tmp_path):
    assert load_econ_calendar(tmp_path / "nope.yaml") == []


def test_load_from_yaml(tmp_path):
    p = tmp_path / "cal.yaml"
    p.write_text("events:\n  - {date: 2026-09-16, event: FOMC, importance: high}\n", encoding="utf-8")
    evs = load_econ_calendar(p)
    assert len(evs) == 1 and evs[0].event == "FOMC" and evs[0].date == date(2026, 9, 16)


def test_committed_seed_loads():
    # the in-package seed must be present and parseable (it ships to the VPS; data/ is gitignored)
    evs = load_econ_calendar(default_calendar_path())
    assert len(evs) >= 1 and all(isinstance(e, EconEvent) for e in evs)
