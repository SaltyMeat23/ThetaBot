"""Per-ticker risk profile: how violent is THIS name's path against a short put, and how wide a
cushion keeps its assignment rate under target.

Built on strike_survival over the bars the scanner already fetches (~1y), computed once per day
per watchlist name -- so a NEW watchlist add is profiled on its first scan, nobody has to remember.
The profile reports touch / ITM / worst-dip at the bot's base cushion and searches a cushion grid
for the smallest one whose historical ITM rate clears ``target_itm``; when that is wider than the
base, the name "needs tightening" and ``propose_ticker_cushions`` turns it into a per-ticker
``min_strike_expected_moves`` suggestion. Tightening-only: a suggestion never loosens an existing
per-ticker override. Note the cushion here is in REALIZED-vol sigma while the gate measures the
candidate's IMPLIED-vol expected move; IV usually exceeds RV, so applying the same number to the
gate is conservative in the safe direction. Pure; no I/O.
"""
from __future__ import annotations

from typing import Any

from .strike_survival import strike_survival

GRID = (0.5, 0.7, 1.0, 1.25, 1.5, 2.0)


def ticker_risk_profile(bars: list[dict], *, base_cushion: float = 0.7, horizon: int = 10,
                        cushions: tuple[float, ...] = GRID, target_itm: float = 0.20,
                        min_n: int = 60) -> dict[str, Any]:
    """Profile one name. ``reliable`` is False (no suggestion) below ``min_n`` entries."""
    grid = tuple(sorted(set(cushions) | {base_cushion}))
    rows = strike_survival({"X": bars}, cushions=grid, horizons=(horizon,), unit="em")
    by = {r["cushion"]: r for r in rows if r["symbol"] == "ALL" and r["label"] == "ALL"}
    base = by.get(base_cushion)
    n = base["n"] if base else 0
    prof: dict[str, Any] = {
        "n": n, "horizon": horizon, "base_cushion": base_cushion, "target_itm": target_itm,
        "touch_rate": base["touch_rate"] if base else None,
        "itm_rate": base["itm_rate"] if base else None,
        "avg_worst_pct": base["avg_worst_pct"] if base else None,
        "by_cushion": [{"cushion": k, "touch_rate": by[k]["touch_rate"], "itm_rate": by[k]["itm_rate"]}
                       for k in grid if k in by],
        "suggested_cushion": None, "needs_tightening": False, "reliable": n >= min_n,
    }
    if n >= min_n:
        ok = [k for k in grid if k in by and k >= base_cushion and by[k]["itm_rate"] <= target_itm]
        sug = min(ok) if ok else max(grid)
        prof["suggested_cushion"] = sug
        prof["needs_tightening"] = sug > base_cushion
    return prof


def propose_ticker_cushions(profiles: dict[str, dict], per_ticker: dict | None, *,
                            base_cushion: float = 0.7, max_cushion: float = 2.0) -> list[dict[str, Any]]:
    """Per-ticker ``min_strike_expected_moves`` suggestions for names whose profile needs tightening.
    Tightening-only: skipped when an existing override is already at least as wide."""
    out: list[dict[str, Any]] = []
    for sym, p in sorted((profiles or {}).items()):
        if not p.get("reliable") or not p.get("needs_tightening"):
            continue
        sug = min(float(p["suggested_cushion"]), max_cushion)
        cur = ((per_ticker or {}).get(sym) or {}).get("min_strike_expected_moves")
        if cur is not None and sug <= float(cur):
            continue
        itm = p.get("itm_rate")
        out.append({
            "symbol": sym, "field": "min_strike_expected_moves", "current": cur, "proposed": sug,
            "reason": (f"{sym}: {itm * 100:.0f}% of puts {base_cushion} sigma below spot finished ITM "
                       f"over {p.get('horizon')} bars (target {p.get('target_itm', 0) * 100:.0f}%); "
                       f"{sug} sigma keeps it under target (n={p.get('n')})"),
        })
    return out
