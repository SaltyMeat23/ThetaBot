"""Dashboard readout of the scanner's technical setup detections (pure builder; mirrors
tv_health.py). Reads the scanner's cached per-symbol reads -- no market-data calls."""
from __future__ import annotations

from typing import Any


def build_setups_view(last_setups: dict, last_context: dict, last_skips: list, entry_cfg,
                      last_scan_at) -> dict[str, Any]:
    """Per-symbol setup labels/flags/features plus which names the setup gates blocked.

    ``last_setups`` is ``{symbol: SetupRead.as_dict() + {"partial_bar": bool}}``; ``last_context``
    is ``{symbol: UnderlyingContext}`` (for price); ``last_skips`` is the scanner's skip list (the
    setup-gate reasons contain the word "setup")."""
    crit = getattr(entry_cfg, "criteria", None)
    setups_cfg = getattr(entry_cfg, "setups", None)
    gate_reason: dict[str, str] = {}
    for s in last_skips or []:
        r = str(s.get("reason") or "")
        if "setup" in r:
            gate_reason.setdefault(str(s.get("symbol")), r)
    counts = {"favorable": 0, "avoid": 0, "mixed": 0, "none": 0}
    symbols: list[dict] = []
    for sym in sorted(last_setups or {}):
        read = last_setups[sym] or {}
        ctx = (last_context or {}).get(sym)
        bias = read.get("bias") or "none"
        counts[bias] = counts.get(bias, 0) + 1
        symbols.append({
            "symbol": sym, "price": getattr(ctx, "price", None),
            "partial_bar": read.get("partial_bar"),
            "setups": read.get("setups") or [], "fired_now": read.get("fired_now") or [],
            "primary": read.get("primary"), "bias": bias, "score": read.get("score", 0) or 0,
            "live": read.get("live") or {}, "flags": read.get("flags") or {},
            "features": read.get("features") or {},
            "gate": {"blocked": sym in gate_reason, "reason": gate_reason.get(sym)},
            "tv": read.get("tv"),   # fresh TradingView flags merged into this read (or None)
        })
    symbols.sort(key=lambda r: (-(r["score"] or 0), r["symbol"]))
    return {
        "as_of": last_scan_at.isoformat() if last_scan_at else None,
        "enabled": bool(getattr(setups_cfg, "enabled", True)),
        "config": {
            "prefer_setups": bool(getattr(entry_cfg, "prefer_setups", False)),
            "avoid_setups": getattr(crit, "avoid_setups", None),
            "require_setups": getattr(crit, "require_setups", None),
            "active_bars": getattr(setups_cfg, "active_bars", None),
        },
        "counts": counts,
        "symbols": symbols,
    }
