"""Strike survival: the probability behind every cash-secured put the bot sells.

For each historical bar as a hypothetical entry, place a put ``k`` cushions below spot and hold it
``H`` bars: how often did price TOUCH the strike intraperiod (roll / assignment pressure) and how
often did it CLOSE below the strike at expiry (assignment)? Cushion units:
  * ``em``  -- multiples of the 1-sigma expected move over H bars, from REALIZED vol (there is no
              historical implied vol offline; since IV usually runs above RV, realized-based
              cushions are slightly conservative for the same dollar distance)
  * ``atr`` -- multiples of ATR(14)
  * ``pct`` -- a flat fraction of spot

For the ``em`` unit the theoretical zero-drift lognormal probabilities are attached --
``p_itm_theory = N(-k)`` (what a delta of that size implies) and ``p_touch_theory = 2*N(-k)``
(reflection principle) -- so realized rates can be read against what the model/greeks assume.
Rows are produced pooled ("ALL" symbol) and per symbol, and optionally conditioned on the setup
label active at entry (``labels_by_symbol``), which is how "which setup keeps my strike safest"
gets answered. Pure; no I/O.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any

from ..entry import indicators

_ND = NormalDist()
WARMUP = 65   # matches entry/setups.MIN_BARS so label conditioning lines up


def _p_itm(k: float) -> float:
    return round(_ND.cdf(-k), 4)


def _p_touch(k: float) -> float:
    return round(min(1.0, 2 * _ND.cdf(-k)), 4)


def strike_survival(
    bars_by_symbol: dict[str, list[dict]], *,
    cushions: tuple[float, ...] = (0.5, 0.7, 1.0, 1.5),
    horizons: tuple[int, ...] = (5, 10, 15),
    unit: str = "em",
    vol_window: int = 20,
    atr_window: int = 14,
    labels_by_symbol: dict[str, dict[int, set[str]]] | None = None,
    min_n: int = 1,
) -> list[dict[str, Any]]:
    """Return rows ``{symbol, label, unit, cushion, horizon, n, touch_rate, itm_rate, avg_worst_pct,
    p_itm_theory, p_touch_theory}`` for symbol="ALL" (pooled) and each symbol, label="ALL" and each
    label active at the entry bar. ``labels_by_symbol[sym][t]`` = labels active on bar index t."""
    acc: dict[tuple, dict] = {}

    def bump(key: tuple, touched: bool, itm: bool, worst: float) -> None:
        a = acc.setdefault(key, {"n": 0, "touch": 0, "itm": 0, "worst": 0.0})
        a["n"] += 1
        a["touch"] += 1 if touched else 0
        a["itm"] += 1 if itm else 0
        a["worst"] += worst

    max_h = max(horizons)
    for sym, bars in bars_by_symbol.items():
        rows_ok = [b for b in bars if b.get("c") is not None]
        closes = [b["c"] for b in rows_ok]
        highs = [b["h"] if b.get("h") is not None else b["c"] for b in rows_ok]
        lows = [b["l"] if b.get("l") is not None else b["c"] for b in rows_ok]
        lab_idx = (labels_by_symbol or {}).get(sym, {})
        warm = max(WARMUP, vol_window + 1, atr_window + 1)
        for t in range(warm, len(closes) - max_h):
            c = closes[t]
            if c <= 0:
                continue
            if unit == "em":
                rv = indicators.realized_vol(closes[:t + 1], vol_window)
                if not rv:
                    continue
                dist = lambda H, k: k * c * rv * math.sqrt(H / 252.0)  # noqa: E731
            elif unit == "atr":
                a = indicators.atr(highs[:t + 1], lows[:t + 1], closes[:t + 1], atr_window)
                if not a:
                    continue
                dist = lambda H, k: k * a  # noqa: E731
            elif unit == "pct":
                dist = lambda H, k: k * c  # noqa: E731
            else:
                raise ValueError(f"unknown cushion unit {unit!r}")
            labels = {"ALL"} | set(lab_idx.get(t, ()))
            for H in horizons:
                window_low = min(lows[t + 1: t + 1 + H])
                end_close = closes[t + H]
                worst = (window_low - c) / c
                for k in cushions:
                    strike = c - dist(H, k)
                    touched = window_low <= strike
                    itm = end_close <= strike
                    for lab in labels:
                        bump(("ALL", lab, k, H), touched, itm, worst)
                        bump((sym, lab, k, H), touched, itm, worst)

    out: list[dict[str, Any]] = []
    for (sym, lab, k, H), a in acc.items():
        if a["n"] < min_n:
            continue
        out.append({
            "symbol": sym, "label": lab, "unit": unit, "cushion": k, "horizon": H, "n": a["n"],
            "touch_rate": round(a["touch"] / a["n"], 4), "itm_rate": round(a["itm"] / a["n"], 4),
            "avg_worst_pct": round(a["worst"] / a["n"], 4),
            "p_itm_theory": _p_itm(k) if unit == "em" else None,
            "p_touch_theory": _p_touch(k) if unit == "em" else None,
        })
    out.sort(key=lambda r: (r["symbol"] != "ALL", r["symbol"], r["label"] != "ALL", r["label"],
                            r["cushion"], r["horizon"]))
    return out


def labels_by_index(bars: list[dict], cfg) -> dict[int, set[str]]:
    """Walk-forward setup labels per bar index (the label set active when THAT bar was the last
    completed bar), for conditioning strike survival on the setup at entry."""
    from ..entry.setups import MIN_BARS, compose, detect_flags
    out: dict[int, set[str]] = {}
    closes_ok = [b for b in bars if b.get("c") is not None]
    for end in range(MIN_BARS, len(closes_ok) + 1):
        flags, _ = detect_flags(closes_ok[:end], cfg)
        labels = compose(flags)
        if labels:
            out[end - 1] = set(labels)
    return out
