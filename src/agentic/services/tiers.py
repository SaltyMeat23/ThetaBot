"""Capital-aware watchlist tiers: which vetted quality names the account can now afford.

``EntryConfig.watchlist_tiers`` maps ticker -> {"min_collateral": price*100 at the time it was
written, "note": "...", "per_ticker": {...overrides applied when added, e.g. a 20% yield floor...}}.
A name is READY when one contract's collateral fits under the per-name cap
(``account_value * max_pct_per_underlying``). The bot only PROPOSES; the user adds with one tap
(the Tuning tab posts the watchlist + the tier's per_ticker overrides). Pure; no I/O.
"""
from __future__ import annotations

from typing import Any


def ready_to_add(tiers: dict[str, dict], watchlist: list[str], account_value: float | None,
                 per_name_pct: float | None, prices: dict[str, float] | None = None,
                 *, backstop_pct: float | None = None) -> list[dict[str, Any]]:
    """Names not on the watchlist whose one-contract collateral fits the per-name cap, cheapest
    first. ``prices`` (live last prices) refresh the stored ``min_collateral`` snapshot."""
    if not tiers or not account_value or account_value <= 0:
        return []
    pct = per_name_pct if per_name_pct is not None else (backstop_pct if backstop_pct is not None else 1.0)
    cap = account_value * pct
    held = {s.upper() for s in (watchlist or [])}
    out: list[dict[str, Any]] = []
    for sym, spec in (tiers or {}).items():
        s = sym.upper()
        if s in held or not isinstance(spec, dict):
            continue
        px = (prices or {}).get(s)
        collateral = (px * 100.0) if px else float(spec.get("min_collateral") or 0.0)
        if collateral <= 0:
            continue
        if collateral <= cap:
            out.append({"symbol": s, "collateral": round(collateral, 2), "per_name_cap": round(cap, 2),
                        "headroom": round(cap - collateral, 2), "note": spec.get("note", ""),
                        "per_ticker": dict(spec.get("per_ticker") or {}), "price_source": "live" if px else "snapshot"})
    out.sort(key=lambda r: r["collateral"])
    return out


def next_unlock(tiers: dict[str, dict], watchlist: list[str], account_value: float | None,
                per_name_pct: float | None, prices: dict[str, float] | None = None) -> dict[str, Any] | None:
    """The cheapest name that does NOT fit yet, with the account value at which it would."""
    if not tiers or not account_value or not per_name_pct:
        return None
    held = {s.upper() for s in (watchlist or [])}
    best = None
    for sym, spec in tiers.items():
        s = sym.upper()
        if s in held or not isinstance(spec, dict):
            continue
        px = (prices or {}).get(s)
        collateral = (px * 100.0) if px else float(spec.get("min_collateral") or 0.0)
        if collateral <= 0 or collateral <= account_value * per_name_pct:
            continue
        need = collateral / per_name_pct
        if best is None or need < best["account_value_needed"]:
            best = {"symbol": s, "collateral": round(collateral, 2), "account_value_needed": round(need, 2),
                    "note": spec.get("note", "")}
    return best
