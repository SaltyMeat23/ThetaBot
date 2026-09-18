"""Setup-accuracy tracking: does a detected pattern actually pay on THESE names?

Pure core (``forward_returns``, ``aggregate_accuracy``, ``replay_history``) plus thin I/O helpers
the scanner calls each cycle (``record_fires``, ``resolve_for_symbol``). Nothing here fetches
market data: forward outcomes are computed from the same daily bars the scanner already pulls,
and the walk-forward replay lets us calibrate on years of history immediately instead of waiting
weeks for live fires.

A "hit" is judged from the premium seller's seat: a FAVORABLE label hit if the stock did NOT fall
over the next 5 bars (ret_5d >= 0); an AVOID label hit if it DID fall (ret_5d < 0). ``mae_10d`` --
the worst excursion below the fire price over 10 bars -- is the number a put seller actually cares
about (how deep did it go against you before/if it recovered).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from ..entry.setups import AVOID, MIN_BARS, compose, detect_flags, label_bias

HORIZONS = (5, 10)
MAX_H = max(HORIZONS)


def _price(b: dict, key: str) -> float | None:
    v = b.get(key)
    return v if v is not None else b.get("c")


def forward_returns(bars: list[dict], bar_date: str, fire_price: float,
                    horizons: tuple[int, ...] = HORIZONS) -> dict[str, float] | None:
    """Forward outcome of a fire on the bar dated ``bar_date`` (bars must carry ``date``).

    Returns ``{ret_5d, ret_10d, mae_10d, mfe_10d}`` as fractions of ``fire_price``, or None until at
    least ``max(horizons)`` completed bars exist AFTER the fire bar (or the date isn't in the bars)."""
    if not fire_price or fire_price <= 0:
        return None
    idx = next((i for i, b in enumerate(bars) if b.get("date") == bar_date), None)
    if idx is None or len(bars) - 1 - idx < MAX_H:
        return None
    out: dict[str, float] = {}
    for h in horizons:
        out[f"ret_{h}d"] = round((bars[idx + h]["c"] - fire_price) / fire_price, 5)
    window = bars[idx + 1: idx + 1 + MAX_H]
    lo = min(_price(b, "l") for b in window)
    hi = max(_price(b, "h") for b in window)
    out[f"mae_{MAX_H}d"] = round(min(0.0, (lo - fire_price) / fire_price), 5)
    out[f"mfe_{MAX_H}d"] = round(max(0.0, (hi - fire_price) / fire_price), 5)
    return out


def _hit(label: str, ret_5d: float) -> bool:
    if label in AVOID:
        return ret_5d < 0
    return ret_5d >= 0   # FAVORABLE (and neutral labels judged the same way)


def _is_episode_start(r: dict) -> bool:
    """True for a fire that OPENS an episode (its label did not fire on the previous bar). Unknown
    (older rows without the flag) counts as a start rather than being dropped."""
    v = r.get("episode_start")
    if v is None and isinstance(r.get("features"), dict):
        v = r["features"].get("episode_start")
    return True if v is None else bool(v)


def _acc() -> dict:
    return {"n": 0, "s5": 0.0, "n5": 0, "s10": 0.0, "n10": 0, "hits": 0, "smae": 0.0, "nmae": 0}


def _add(a: dict, label: str, r5, r10, mae) -> None:
    a["n"] += 1
    if r5 is not None:
        a["s5"] += r5
        a["n5"] += 1
        a["hits"] += 1 if _hit(label, r5) else 0
    if r10 is not None:
        a["s10"] += r10
        a["n10"] += 1
    if mae is not None:
        a["smae"] += mae
        a["nmae"] += 1


def _stats(a: dict, prefix: str = "") -> dict:
    return {
        f"{prefix}hit_rate_5d": round(a["hits"] / a["n5"], 3) if a["n5"] else None,
        f"{prefix}avg_ret_5d": round(a["s5"] / a["n5"], 4) if a["n5"] else None,
        f"{prefix}avg_ret_10d": round(a["s10"] / a["n10"], 4) if a["n10"] else None,
        f"{prefix}avg_mae_10d": round(a["smae"] / a["nmae"], 4) if a["nmae"] else None,
    }


def aggregate_accuracy(rows: list[dict]) -> list[dict[str, Any]]:
    """Per (label, source): n resolved / pending, hit_rate_5d, avg 5/10-day returns, avg MAE -- for
    ALL fires and, under the ``ep_`` prefix, for EPISODE STARTS only. A setup that stays active for
    days re-fires every bar with overlapping forward windows, so raw ``n`` overstates the evidence;
    ``n_ep`` (first bar of each run) is the honest count. Sorted by n desc."""
    groups: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("label"), r.get("source"))
        g = groups.setdefault(key, {"label": key[0], "source": key[1], "n_pending": 0,
                                    "all": _acc(), "ep": _acc()})
        if r.get("resolved_at") is None:
            g["n_pending"] += 1
            continue
        r5, r10, mae = r.get("ret_5d"), r.get("ret_10d"), r.get("mae_10d")
        _add(g["all"], key[0], r5, r10, mae)
        if _is_episode_start(r):
            _add(g["ep"], key[0], r5, r10, mae)
    out = []
    for g in groups.values():
        row = {"label": g["label"], "source": g["source"], "n": g["all"]["n"],
               "n_pending": g["n_pending"], "n_ep": g["ep"]["n"],
               "bias": label_bias(g["label"])}
        row.update(_stats(g["all"]))
        row.update(_stats(g["ep"], "ep_"))
        out.append(row)
    out.sort(key=lambda x: (-x["n"], x["label"]))
    return out


# --- live tracking (called by the scanner each cycle; advisory) --------------------------------

def record_fires(store, symbol: str, read, bars: list[dict], now: datetime, *, partial: bool,
                 source: str = "bot_daily", prev_labels: set | None = None) -> int:
    """Record each ``fired_now`` label for the last COMPLETED bar. Repeated scans of the same bar are
    no-ops (unique index). Never records from a partial bar. ``prev_labels`` (the labels that fired
    on the bar before it) stamps ``features.episode_start`` so accuracy can count episodes, not
    consecutive re-fires. Returns rows inserted."""
    if read is None or not getattr(read, "fired_now", None) or not bars:
        return 0
    completed = bars[:-1] if (partial and len(bars) > 1) else bars
    bar = completed[-1]
    price = bar.get("c")
    if price is None or price <= 0:
        return 0
    bar_date = bar.get("date") or (now.date() - timedelta(days=1 if partial else 0)).isoformat()
    feat = {k: v for k, v in (read.features or {}).items() if k != "active_bars_ago"}
    n = 0
    for label in read.fired_now:
        f = dict(feat)
        f["episode_start"] = (label not in prev_labels) if prev_labels is not None else None
        if store.record(symbol=symbol, label=label, bar_date=bar_date, source=source,
                        fire_price=float(price), features=f, fired_at=now):
            n += 1
    return n


def resolve_for_symbol(store, symbol: str, bars: list[dict], now: datetime) -> int:
    """Fill in forward outcomes for pending fires once enough completed bars exist. Providers whose
    bars carry no ``date`` (Robinhood) fall back to a calendar rule: after >= 14 days, ret_10d is
    approximated from the latest close and mae/mfe are left None."""
    dated = any(b.get("date") for b in bars)
    n = 0
    for ev in store.pending(symbol=symbol):
        fr = forward_returns(bars, ev["bar_date"], ev["fire_price"]) if dated else None
        if fr is None and not dated and bars:
            try:
                fired = datetime.fromisoformat(ev["fired_at"])
            except (TypeError, ValueError):
                continue
            if (now - fired) >= timedelta(days=14) and bars[-1].get("c"):
                fp = ev["fire_price"]
                fr = {"ret_5d": None, "ret_10d": round((bars[-1]["c"] - fp) / fp, 5),
                      "mae_10d": None, "mfe_10d": None}
        if fr is None:
            continue
        store.resolve(ev["id"], ret_5d=fr.get("ret_5d"), ret_10d=fr.get("ret_10d"),
                      mae_10d=fr.get("mae_10d"), mfe_10d=fr.get("mfe_10d"), resolved_at=now)
        n += 1
    return n


# --- historical replay (calibrate on years of bars right now) ----------------------------------

def replay_history(bars_by_symbol: dict[str, list[dict]], cfg, *,
                   horizons: tuple[int, ...] = HORIZONS) -> list[dict[str, Any]]:
    """Walk forward through each symbol's dated daily bars, fire the detectors on every completed
    bar exactly as the scanner would, and attach the realized forward outcome. Structural levels
    fall back to the Donchian range (no TradingView history exists offline). Returns rows shaped
    like setup_events (source="replay", already resolved) for ``aggregate_accuracy``."""
    rows: list[dict[str, Any]] = []
    for symbol, bars in bars_by_symbol.items():
        bars = [b for b in bars if b.get("c") is not None and b.get("date")]
        prev_labels: set[str] = set()
        for end in range(MIN_BARS, len(bars) - MAX_H + 1):
            window = bars[:end]
            flags, feat = detect_flags(window, cfg)
            labels = compose(flags)
            bar = window[-1]
            fr = forward_returns(bars, bar["date"], bar["c"], horizons) if labels else None
            if fr is not None:
                for label in labels:
                    rows.append({"symbol": symbol, "label": label, "bar_date": bar["date"],
                                 "source": "replay", "fire_price": bar["c"], "resolved_at": "replay",
                                 "episode_start": label not in prev_labels,
                                 "features": {k: feat.get(k) for k in ("rsi", "bb_width_pct", "vol_ratio_20")},
                                 **fr})
            prev_labels = set(labels)
    return rows
