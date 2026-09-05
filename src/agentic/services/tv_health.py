"""TradingView-indicator freshness health.

The scanner/AI reads support/resistance from TradingView webhook alerts, but nothing watched
whether those alerts keep arriving. A watchlist name whose indicator silently goes stale (past
``ai.tv_indicator_max_age_seconds``) is invisible to the AI yet the scan looks healthy. This
builds a per-symbol freshness report so a stale/missing feed for a TRADED name is caught.
"""
from __future__ import annotations

from typing import Any


def build_tv_health(store, watchlist: list[str], max_age_seconds: int) -> dict[str, Any]:
    """Per-symbol indicator freshness for the watchlist, plus stored-but-unwatched cruft.

    ``store`` is a TVIndicatorStore (or anything exposing ``recent()`` with symbol/age_seconds).
    Returns:
      threshold_seconds — the staleness cutoff (ai.tv_indicator_max_age_seconds)
      symbols           — [{symbol, present, age_seconds, stale}] for each watchlist name
      stale             — watchlist symbols present but older than the threshold
      missing           — watchlist symbols with no indicator stored at all
      fresh_count       — watchlist symbols present and within the threshold
      cruft             — stored symbols NOT in the watchlist (leftover from prior watchlists)
    """
    wl = [s.upper() for s in (watchlist or [])]
    wl_set = set(wl)
    stored = {r["symbol"].upper(): r for r in (store.recent(500) if store else [])}

    # Most recent webhook overall (any symbol) — for a "last received" readout.
    latest = None
    for r in stored.values():
        if latest is None or r["received_at"] > latest["received_at"]:
            latest = r
    latest_out = None if latest is None else {
        "symbol": latest["symbol"].upper(), "received_at": latest["received_at"],
        "age_seconds": latest["age_seconds"],
    }

    symbols: list[dict] = []
    stale: list[str] = []
    missing: list[str] = []
    fresh = 0
    for sym in wl:
        r = stored.get(sym)
        if r is None:
            missing.append(sym)
            symbols.append({"symbol": sym, "present": False, "age_seconds": None, "stale": True,
                            "received_at": None, "support": None, "resistance": None})
            continue
        age = r["age_seconds"]
        is_stale = age > max_age_seconds
        pay = r.get("payload", {}) or {}
        symbols.append({
            "symbol": sym, "present": True, "age_seconds": age, "stale": is_stale,
            "received_at": r["received_at"], "support": pay.get("support"),
            "resistance": pay.get("resistance"),
        })
        if is_stale:
            stale.append(sym)
        else:
            fresh += 1

    cruft = sorted(s for s in stored if s not in wl_set)
    return {
        "threshold_seconds": max_age_seconds,
        "symbols": symbols,
        "stale": stale,
        "missing": missing,
        "fresh_count": fresh,
        "cruft": cruft,
        "latest": latest_out,
        "ok": not stale and not missing,
    }


def tv_health_digest_line(health: dict) -> str | None:
    """A one-line digest warning when watchlist indicators are stale/missing, else None."""
    problems = list(health.get("missing", [])) + list(health.get("stale", []))
    if not problems:
        return None
    hrs = health.get("threshold_seconds", 0) / 3600
    return f"! TV indicators stale/missing (> {hrs:.0f}h): {', '.join(problems[:8])}"
