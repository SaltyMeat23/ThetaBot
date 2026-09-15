"""Curated economic-event calendar for the weekly tactical brief.

A maintained STATIC schedule (FOMC decisions, CPI/PPI/NFP/retail sales). Refresh ~yearly from the
official Fed (FOMC) and BLS release schedules. The committed seed ships in-package
(econ_calendar_seed.yaml); an optional writable override at <repo>/data/econ_calendar.yaml
(gitignored — so you can edit it on the VPS without a redeploy) takes precedence when present.

Fail-open everywhere: a missing/blank/malformed file yields an empty calendar and the brief still
renders. Pure — no network.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

log = logging.getLogger("agentic.econ_calendar")

_IMPORTANCE = {"high", "medium", "low"}


@dataclass(frozen=True)
class EconEvent:
    date: date
    event: str
    importance: str = "medium"


def _coerce_date(v) -> date | None:
    if isinstance(v, date):
        return v
    if isinstance(v, str):
        try:
            return date.fromisoformat(v.strip())
        except ValueError:
            return None
    return None


def parse_events(raw) -> list[EconEvent]:
    """Turn a list of {date, event, importance} dicts into EconEvents. Skips malformed rows."""
    out: list[EconEvent] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        d = _coerce_date(row.get("date"))
        name = row.get("event")
        if d is None or not name:
            continue
        imp = str(row.get("importance", "medium")).lower()
        out.append(EconEvent(d, str(name), imp if imp in _IMPORTANCE else "medium"))
    return out


def default_calendar_path() -> Path:
    """Writable override at data/econ_calendar.yaml (gitignored) if present, else the committed seed."""
    from ..config import REPO_ROOT
    override = REPO_ROOT / "data" / "econ_calendar.yaml"
    return override if override.exists() else Path(__file__).with_name("econ_calendar_seed.yaml")


def load_econ_calendar(path=None) -> list[EconEvent]:
    """Load events from YAML (a bare list, or {events: [...]}). Fail-open to []."""
    p = Path(path) if path is not None else default_calendar_path()
    try:
        import yaml
        with open(p, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — never break the brief on a bad calendar file
        log.warning("econ calendar load failed (%s): %s", p, exc)
        return []
    if isinstance(raw, dict):
        raw = raw.get("events")
    return parse_events(raw)


def upcoming_events(events: list[EconEvent], today: date, days: int = 7) -> list[EconEvent]:
    """Events falling within [today, today+days], sorted by date then name."""
    end = today + timedelta(days=days)
    return sorted((e for e in events if today <= e.date <= end), key=lambda e: (e.date, e.event))
