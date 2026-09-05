"""Descriptive entry-feature analytics over resolved trades.

Reads the refinement rows (the feature->outcome table from ``services.refinement``) and reports
win-rate + average realized P&L bucketed by entry feature: ticker, strategy kind, delta band, DTE,
IV, RSI, 200-SMA trend, market regime, AI verdict, and exit rule.

Pure and side-effect-free. This is DESCRIPTIVE, not predictive: with few trades every bucket is
small and noisy, so read it as hints, not truth. It sharpens automatically as the trade journal
grows — and it is exactly the table a win-probability model would eventually train on.
"""
from __future__ import annotations

from typing import Any, Callable


def _is_win(row: dict[str, Any]) -> bool:
    pnl = row.get("realized_pnl")
    return pnl is not None and pnl > 0


def _band(value: float | None, edges: list[float], labels: list[str]) -> str | None:
    """Map a numeric value to a band label. ``labels`` must have ``len(edges) + 1`` entries."""
    if value is None:
        return None
    for i, edge in enumerate(edges):
        if value < edge:
            return labels[i]
    return labels[-1]


def _ctx(row: dict[str, Any]) -> dict[str, Any]:
    c = row.get("context")
    return c if isinstance(c, dict) else {}


def _abs_delta(row: dict[str, Any]) -> float | None:
    d = row.get("delta")
    return abs(d) if d is not None else None


# Each dimension maps a row to a bucket label (or None to exclude it from that dimension).
_DIMENSIONS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "underlying": lambda r: r.get("underlying"),
    "kind": lambda r: r.get("kind"),
    "delta": lambda r: _band(_abs_delta(r), [0.15, 0.20, 0.25, 0.30],
                             ["<0.15", "0.15-0.20", "0.20-0.25", "0.25-0.30", "0.30+"]),
    "dte": lambda r: _band(r.get("dte"), [8, 15, 31], ["0-7", "8-14", "15-30", "31+"]),
    "iv": lambda r: _band(r.get("iv"), [0.5, 0.8, 1.1], ["<0.50", "0.50-0.80", "0.80-1.10", "1.10+"]),
    "rsi": lambda r: _band(_ctx(r).get("rsi"), [30, 45, 55, 70],
                           ["<30", "30-45", "45-55", "55-70", "70+"]),
    "above_sma200": lambda r: (None if _ctx(r).get("above_sma200") is None
                               else ("above" if _ctx(r).get("above_sma200") else "below")),
    "adx": lambda r: _band(_ctx(r).get("adx"), [20, 25, 30, 40],
                           ["<20", "20-25", "25-30", "30-40", "40+"]),
    "bb_percent_b": lambda r: _band(_ctx(r).get("bb_percent_b"), [20, 40, 60, 80],
                                    ["<20", "20-40", "40-60", "60-80", "80+"]),
    "iv_rank": lambda r: _band(_ctx(r).get("iv_rank"), [25, 50, 75],
                               ["<25", "25-50", "50-75", "75+"]),
    "iv_rv_ratio": lambda r: _band(_ctx(r).get("iv_rv_ratio"), [0.9, 1.1, 1.4],
                                   ["<0.9", "0.9-1.1", "1.1-1.4", "1.4+"]),
    "mkt_regime": lambda r: _ctx(r).get("mkt_regime"),   # market regime at entry (calm/elevated/risk_off)
    "mfe_pct": lambda r: _band(_ctx(r).get("mfe_pct"), [0.25, 0.5, 0.75],
                               ["<25%", "25-50%", "50-75%", "75%+"]),   # best profit the trade reached
    "mae_pct": lambda r: _band(_ctx(r).get("mae_pct"), [-0.5, -0.25, 0.0],
                               ["<-50%", "-50--25%", "-25-0%", "0%+"]),  # worst drawdown the trade saw
    "regime": lambda r: r.get("ai_regime_label"),
    "ai_recommendation": lambda r: r.get("ai_recommendation"),
    "exit_reason": lambda r: r.get("exit_reason"),
}


def _group_stats(rows: list[dict[str, Any]], key_fn: Callable[[dict], Any]) -> list[dict[str, Any]]:
    """Win-rate + P&L stats per bucket for one dimension (rows with a None key are skipped)."""
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        key = key_fn(row)
        if key is None:
            continue
        groups.setdefault(key, []).append(row)
    out: list[dict[str, Any]] = []
    for key, grp in groups.items():
        n = len(grp)
        wins = sum(1 for r in grp if _is_win(r))
        pnl = sum((r.get("realized_pnl") or 0.0) for r in grp)
        out.append({
            "bucket": key,
            "n": n,
            "wins": wins,
            "win_rate": round(wins / n, 3) if n else None,
            "avg_pnl": round(pnl / n, 2) if n else None,
            "total_pnl": round(pnl, 2),
        })
    return sorted(out, key=lambda x: str(x["bucket"]))


def build_feature_analytics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Win-rate + avg realized P&L bucketed by entry feature, over RESOLVED trades only.

    ``rows`` are refinement rows (see ``services.refinement.build_refinement_rows``). A trade counts
    as resolved when it has a non-null ``realized_pnl``; a win is ``realized_pnl > 0``.
    """
    resolved = [r for r in rows if r.get("realized_pnl") is not None]
    n = len(resolved)
    wins = sum(1 for r in resolved if _is_win(r))
    total = sum((r.get("realized_pnl") or 0.0) for r in resolved)
    summary = {
        "resolved_trades": n,
        "wins": wins,
        "losses": n - wins,
        "win_rate": round(wins / n, 3) if n else None,
        "total_pnl": round(total, 2),
        "avg_pnl": round(total / n, 2) if n else None,
    }
    by_feature = {name: _group_stats(resolved, fn) for name, fn in _DIMENSIONS.items()}
    return {
        "summary": summary,
        "by_feature": by_feature,
        "note": ("Descriptive, not predictive. Buckets are small and noisy until many trades "
                 "accumulate (~200+ for stable signal) — read as hints. Sharpens automatically as "
                 "the trade journal grows; this is also the table a win-probability model trains on."),
    }
