"""Deterministic technical setup detection on daily bars (pure; no I/O).

Turns the bars the scanner already fetches into named, testable pattern reads the bot can consume
ON ITS OWN -- the operator is usually away and cannot act on alerts, so detection has to feed the
scanner directly (opt-in gates + ranking tilt + AI-reviewer context) and be journaled so the
analytics flywheel learns which patterns actually have edge for these names.

Families (all four the operator asked for):
  * washout          -- oversold / mean-reversion: the classic "sell a put into the flush" timing
  * coiling          -- a volatility squeeze (Bollinger inside Keltner, or width at a multi-week low):
                        the setup BEFORE a breakout
  * breakout/breakdown (+_confirmed on volume) -- a range break actually happening
  * support tests    -- price holding a level (rejection candle), on volume or not

Labels split into FAVORABLE (for a premium seller) and AVOID (a fresh breakdown / support break /
falling knife -- do not sell puts into it). Every atomic flag is None (unknown) rather than False
when its data is missing (e.g. a provider with no volume), so downstream gates fail open.

Detection runs on COMPLETED bars. The live partial bar is read separately into ``live`` (the
``*_attempt`` flags) so "something is happening right now" is visible within one scan cycle without
letting an unfinished bar confirm a pattern. No look-ahead: range and volume references exclude the
bar being tested.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

from . import indicators

# Classification is from the PREMIUM SELLER's seat (this bot sells puts), calibrated on the 2y x 8-name
# strike-survival replay (2026-09-15): FAVORABLE = entries where a short strike is least likely to be
# assigned / run over; AVOID = entries with the highest assignment rate or deepest dips. Note the
# counter-intuitive part the data forced: BREAKOUTS are AVOID for put selling -- on these
# mean-reverting names every breakout label finished ITM ~30-35% vs a 23% base rate. A directional
# (long) trader would read the breakout labels the other way; the labels themselves are neutral facts.
FAVORABLE = ("support_test_rejection", "support_test_on_volume", "quiet_base",
             "washout_at_support", "washout")
AVOID = ("breakdown_confirmed", "support_break", "breakdown", "falling_knife",
         "breakout_from_base", "breakout_followthrough", "climax_breakout", "breakout_confirmed",
         "breakout_strong", "breakout")
NEUTRAL = ("coiling", "coiling_near_resistance")   # informational: a squeeze says "something's coming", not which way
PRIORITY = AVOID + FAVORABLE + NEUTRAL             # primary label = first match (avoid wins)
ALL_LABELS = PRIORITY
# The 2026-09 candidates graduated after replay validation (breakout_* -> AVOID, quiet_base ->
# FAVORABLE). Nothing is currently under validation; add new hypotheses here (they stay neutral).
CANDIDATE: tuple[str, ...] = ()
# One-tap Tuning-tab preset: skip selling puts into the breakout family (the measured worst timing).
PUT_SELLER_AVOID_PRESET = ("breakout", "breakout_confirmed", "breakout_strong",
                           "breakout_followthrough", "breakout_from_base", "climax_breakout")
MIN_BARS = 65


def label_bias(label: str) -> str:
    """favorable | avoid | neutral | unknown -- the single source of truth every consumer uses."""
    if label in FAVORABLE:
        return "favorable"
    if label in AVOID:
        return "avoid"
    if label in NEUTRAL:
        return "neutral"
    return "unknown"                  # ~ squeeze lookback + Bollinger window: enough for every detector
_NO_LIVE = {"breakout_attempt": None, "breakdown_attempt": None, "support_break_attempt": None}


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _t(v: Any) -> bool:
    return v is True


@dataclass
class SetupRead:
    flags: dict[str, Any] = field(default_factory=dict)      # atomic conditions; None = unknown
    features: dict[str, Any] = field(default_factory=dict)   # the numerics behind the flags
    setups: list[str] = field(default_factory=list)          # labels ACTIVE (fired within active_bars)
    fired_now: list[str] = field(default_factory=list)       # labels true on the last completed bar
    live: dict[str, Any] = field(default_factory=dict)       # partial-bar view: *_attempt flags
    bias: str = "none"                                       # favorable | avoid | mixed | none
    score: int = 0                                           # +1 per favorable, -2 per avoid
    primary: str | None = None

    def as_dict(self) -> dict:
        return {"flags": self.flags, "features": self.features, "setups": self.setups,
                "fired_now": self.fired_now, "live": self.live, "bias": self.bias,
                "score": self.score, "primary": self.primary}


def _series(bars: list[dict]) -> tuple[list, list, list, list, list]:
    rows = [b for b in bars if b.get("c") is not None]
    closes = [b["c"] for b in rows]
    highs = [b["h"] if b.get("h") is not None else b["c"] for b in rows]
    lows = [b["l"] if b.get("l") is not None else b["c"] for b in rows]
    opens = [b["o"] if b.get("o") is not None else b["c"] for b in rows]
    vols = [b.get("v") for b in rows]
    return closes, highs, lows, opens, vols


def _level(explicit: Any, fallback: float | None, src: str) -> tuple[float | None, str | None]:
    """A structural reference: the fresh TradingView level when present, else the Donchian one."""
    if _num(explicit) and explicit > 0:
        return float(explicit), "tv"
    return (fallback, src) if fallback is not None else (None, None)


def bars_since_range_break(highs: list, lows: list, closes: list, n: int = 20,
                           max_look: int = 60) -> tuple[int, str | None]:
    """Walking back from the LAST bar of the given series: how many bars since a close last landed
    outside its prior-n-bar Donchian range, and which way ("up"/"down"). ``(max_look, None)`` when
    no break is found within ``max_look`` bars. Call it on the series EXCLUDING the bar being
    tested to measure how long a base preceded a break."""
    count = 0
    for end in range(len(closes), n, -1):          # window ending at index end-1
        if count >= max_look:
            return max_look, None
        don = indicators.donchian(highs[:end], lows[:end], n, exclude_last=True)
        if don is None:
            break
        c = closes[end - 1]
        if c > don[1]:
            return count, "up"
        if c < don[0]:
            return count, "down"
        count += 1
    return count, None


def detect_flags(bars: list[dict], cfg, *, support: float | None = None,
                 resistance: float | None = None) -> tuple[dict, dict]:
    """Atomic flags + features for the LAST bar of ``bars`` (which must be COMPLETED bars)."""
    closes, highs, lows, opens, vols = _series(bars)
    flags: dict[str, Any] = {}
    feat: dict[str, Any] = {}
    if len(closes) < MIN_BARS:
        return flags, feat
    c, h, l, o = closes[-1], highs[-1], lows[-1], opens[-1]
    rsi = indicators.rsi(closes, 14)
    sma20 = indicators.sma(closes, 20)
    a = indicators.atr(highs, lows, closes, 14)
    bb = indicators.bollinger(closes, cfg.bb_len, cfg.bb_std)
    pb = indicators.bb_percent_b(closes, cfg.bb_len, cfg.bb_std)
    width = indicators.bb_width_pct(closes, cfg.bb_len, cfg.bb_std)
    wseries = indicators.bb_width_series(closes, cfg.bb_len, cfg.bb_std, cfg.squeeze_lookback)
    kc = indicators.keltner(highs, lows, closes, cfg.kc_len, cfg.kc_mult)
    don = indicators.donchian(highs, lows, cfg.donchian_len, exclude_last=True)
    vr = indicators.volume_ratio(vols, cfg.vol_avg_len)

    sup_ref, sup_src = _level(support, don[0] if don else None, "donchian")
    res_ref, res_src = _level(resistance, don[1] if don else None, "donchian")

    feat.update({
        "rsi": rsi, "sma20": sma20, "atr": a, "bb_percent_b": pb, "bb_width_pct": width,
        "donchian_high_20": don[1] if don else None, "donchian_low_20": don[0] if don else None,
        "vol_ratio_20": vr, "support_ref": sup_ref, "support_source": sup_src,
        "resistance_ref": res_ref, "resistance_source": res_src,
        "dist_to_support_pct": round((c - sup_ref) / sup_ref * 100, 2) if sup_ref else None,
        "dist_to_resistance_pct": round((res_ref - c) / res_ref * 100, 2) if res_ref else None,
    })

    flags["oversold"] = (rsi <= cfg.rsi_oversold) if rsi is not None else None
    flags["below_lower_bb"] = (c < bb[0]) if bb else None
    flags["stretched_below_ma"] = (((sma20 - c) / a) >= cfg.stretch_atr) if (sma20 is not None and a) else None
    prior_w = wseries[:-1]
    flags["bb_squeeze"] = (width <= min(prior_w)) if (width is not None and prior_w) else None
    flags["ttm_squeeze"] = (bb[2] < kc[2] and bb[0] > kc[0]) if (bb and kc) else None
    flags["near_resistance"] = ((0 <= (res_ref - c) / res_ref <= cfg.resistance_proximity_pct)
                                if res_ref else None)
    if sup_ref:
        near = abs(c - sup_ref) / sup_ref <= cfg.support_proximity_pct
        pierced = l <= sup_ref * (1 + cfg.support_proximity_pct) <= h
        flags["near_support"] = bool(near or pierced)
        flags["support_break"] = c < sup_ref * (1 - cfg.support_proximity_pct)
    else:
        flags["near_support"] = None
        flags["support_break"] = None
    rng = h - l
    flags["rejection_candle"] = ((((c - l) / rng) >= cfg.rejection_close_pos) and c >= o) if rng > 0 else False
    flags["volume_breakout"] = (vr >= cfg.breakout_vol_ratio) if vr is not None else None
    flags["volume_anomaly"] = (vr >= cfg.anomaly_vol_ratio) if vr is not None else None
    flags["broke_range_high"] = (c > don[1]) if don else None
    flags["broke_range_low"] = (c < don[0]) if don else None

    # --- candidate flags under validation (see CANDIDATE) --------------------------------------
    flags["volume_strong"] = (vr >= cfg.strong_vol_ratio) if vr is not None else None
    # did the PREVIOUS bar break its own prior range? (follow-through = two breaks running)
    prev_don = (indicators.donchian(highs[:-1], lows[:-1], cfg.donchian_len, exclude_last=True)
                if len(closes) > cfg.donchian_len + 2 else None)
    flags["prev_broke_range_high"] = (closes[-2] > prev_don[1]) if prev_don else None
    # how long a base preceded THIS bar (bars since the last range break, before this bar)
    since, last_dir = bars_since_range_break(highs[:-1], lows[:-1], closes[:-1],
                                             cfg.donchian_len, cfg.base_lookback)
    feat["bars_since_range_break"], feat["last_break_dir"] = since, last_dir
    flags["from_base"] = since >= cfg.base_min_bars
    flags["climax"] = (last_dir == "up") and (2 <= since <= cfg.climax_max_bars)
    # quiet base: a tight recent range, at/below the 50-day, on quiet volume (the slow-grind
    # accumulation zone the sharper detectors never see)
    qn = cfg.quiet_base_bars
    sma50 = indicators.sma(closes, 50)
    if len(closes) > qn + 50 and sma50 is not None:
        # Range on CLOSES (wick-robust: a single spike day inside the window must not hide a
        # base), price at/near-or-below the 50-day, quiet volume.
        rng = (max(closes[-qn:]) - min(closes[-qn:])) / c
        feat["base_range_pct"] = round(rng * 100, 2)
        flags["quiet_base"] = bool(rng <= cfg.quiet_base_range_pct and c <= sma50 * 1.03
                                   and (vr is None or vr <= 1.0))
    else:
        flags["quiet_base"] = None
    return flags, feat


def compose(flags: dict) -> list[str]:
    """Composite labels from atomic flags, PRIORITY-ordered (so [0] is the primary)."""
    labels: set[str] = set()
    votes = sum(1 for k in ("oversold", "below_lower_bb", "stretched_below_ma") if _t(flags.get(k)))
    washout = votes >= 2
    if washout:
        labels.add("washout")
        if _t(flags.get("near_support")):
            labels.add("washout_at_support")
        if _t(flags.get("broke_range_low")):
            labels.add("falling_knife")
    coiling = _t(flags.get("bb_squeeze")) or _t(flags.get("ttm_squeeze"))
    if coiling:
        labels.add("coiling")
        if _t(flags.get("near_resistance")):
            labels.add("coiling_near_resistance")
    if _t(flags.get("broke_range_high")):
        labels.add("breakout_confirmed" if _t(flags.get("volume_breakout")) else "breakout")
        # candidate refinements (neutral until validated)
        if _t(flags.get("volume_strong")):
            labels.add("breakout_strong")
        if _t(flags.get("prev_broke_range_high")):
            labels.add("breakout_followthrough")
        if _t(flags.get("from_base")):
            labels.add("breakout_from_base")
        if _t(flags.get("climax")):
            labels.add("climax_breakout")
    if _t(flags.get("quiet_base")):
        labels.add("quiet_base")
    if _t(flags.get("broke_range_low")):
        labels.add("breakdown_confirmed" if _t(flags.get("volume_breakout")) else "breakdown")
    if _t(flags.get("near_support")) and _t(flags.get("rejection_candle")):
        labels.add("support_test_rejection")
        if _t(flags.get("volume_anomaly")):
            labels.add("support_test_on_volume")
    if _t(flags.get("support_break")):
        labels.add("support_break")
    return [x for x in PRIORITY if x in labels] + sorted(x for x in labels if x not in PRIORITY)


def active_setups(bars: list[dict], cfg, *, support: float | None = None,
                  resistance: float | None = None) -> dict[str, int]:
    """label -> bars_ago (0 = last completed bar) for labels that fired on ANY of the last
    ``cfg.active_bars`` completed bars -- a setup stays 'active' for a few sessions."""
    out: dict[str, int] = {}
    for ago in range(max(1, int(cfg.active_bars))):
        end = len(bars) - ago
        if end < MIN_BARS:
            break
        flags, _ = detect_flags(bars[:end], cfg, support=support, resistance=resistance)
        for lab in compose(flags):
            out.setdefault(lab, ago)
    return out


def live_flags(all_bars: list[dict], cfg, *, support: float | None = None,
               resistance: float | None = None) -> dict:
    """Partial-bar view: does TODAY's unfinished bar break the range / support right now? Volume
    is deliberately NOT read (a partial day understates it), so nothing here can CONFIRM a pattern."""
    closes, highs, lows, _o, _v = _series(all_bars)
    out = dict(_NO_LIVE)
    if len(closes) < MIN_BARS:
        return out
    c = closes[-1]
    don = indicators.donchian(highs, lows, cfg.donchian_len, exclude_last=True)
    if don:
        out["breakout_attempt"] = c > don[1]
        out["breakdown_attempt"] = c < don[0]
    sup_ref, _src = _level(support, don[0] if don else None, "donchian")
    if sup_ref:
        out["support_break_attempt"] = c < sup_ref * (1 - cfg.support_proximity_pct)
    return out


def setup_score(labels: list[str]) -> int:
    return sum(1 for x in labels if x in FAVORABLE) - 2 * sum(1 for x in labels if x in AVOID)


def setup_bias(labels: list[str]) -> str:
    fav = any(x in FAVORABLE for x in labels)
    av = any(x in AVOID for x in labels)
    return "mixed" if (fav and av) else "favorable" if fav else "avoid" if av else "none"


def primary_setup(labels: list[str]) -> str | None:
    for x in PRIORITY:
        if x in labels:
            return x
    return labels[0] if labels else None


def detect_setups(bars: list[dict], cfg, *, support: float | None = None,
                  resistance: float | None = None, last_bar_partial: bool = False) -> SetupRead | None:
    """Full read for one name. ``last_bar_partial`` (market open) drops the unfinished last bar from
    the confirmed view and populates ``live`` from it. None when there is too little history."""
    if not bars:
        return None
    completed = bars[:-1] if (last_bar_partial and len(bars) > 1) else bars
    if len(completed) < MIN_BARS:
        return None
    flags, feat = detect_flags(completed, cfg, support=support, resistance=resistance)
    fired_now = compose(flags)
    active = active_setups(completed, cfg, support=support, resistance=resistance)
    setups = [x for x in PRIORITY if x in active] + sorted(x for x in active if x not in PRIORITY)
    live = (live_flags(bars, cfg, support=support, resistance=resistance)
            if last_bar_partial else dict(_NO_LIVE))
    feat["active_bars_ago"] = active
    return SetupRead(flags=flags, features=feat, setups=setups, fired_now=fired_now, live=live,
                     bias=setup_bias(setups), score=setup_score(setups),
                     primary=primary_setup(setups))


# --- TradingView real-time layer ----------------------------------------------------------------

TV_DAILY_FLAGS = ("squeeze_on", "breakout", "breakdown")
TV_DAILY_NUMERIC = ("vol_ratio_20",)
TV_INTRADAY_FLAGS = ("i_breakout_attempt", "i_breakdown_attempt", "i_support_test", "i_vol_anomaly")
TV_FLAG_KEYS = TV_DAILY_FLAGS + TV_INTRADAY_FLAGS


def _wire_num(v: Any) -> float | None:
    return float(v) if _num(v) else None


def parse_tv_setups(payload: dict, *, now_ms: int, daily_max_seconds: int = 172800,
                    intraday_mult: float = 2.0) -> tuple[dict[str, Any], float | None]:
    """Extract FRESH TradingView setup flags from the bot's merged webhook snapshot.

    Freshness is judged from the PAYLOAD bar times -- ``d_bar_time`` for the Daily exporter (valid
    for ``daily_max_seconds``) and ``i_bar_time`` + ``i_tf`` minutes for the intraday one (valid for
    ``intraday_mult`` x tf) -- never from the snapshot's ``received_at``, which every alert bumps, so
    stale keys linger there. Booleans must arrive as 0/1 numerics; JSON true/false is rejected
    (consistent with the adx/%B overlay). Returns ``(flags, youngest_bar_age_seconds)``."""
    out: dict[str, Any] = {}
    ages: list[float] = []
    d_t = _wire_num(payload.get("d_bar_time"))
    if d_t is not None and 0 <= (now_ms - d_t) <= daily_max_seconds * 1000:
        for k in TV_DAILY_FLAGS:
            v = _wire_num(payload.get(k))
            if v is not None:
                out[k] = bool(int(v))
        for k in TV_DAILY_NUMERIC:
            v = _wire_num(payload.get(k))
            if v is not None:
                out[k] = v
        ages.append((now_ms - d_t) / 1000)
    i_t = _wire_num(payload.get("i_bar_time"))
    tf = _wire_num(payload.get("i_tf")) or 30.0
    if i_t is not None and 0 <= (now_ms - i_t) <= intraday_mult * tf * 60 * 1000:
        for k in TV_INTRADAY_FLAGS:
            v = _wire_num(payload.get(k))
            if v is not None:
                out[k] = bool(int(v))
        ages.append((now_ms - i_t) / 1000)
    return out, (round(min(ages), 1) if ages else None)


def merge_tv(setups: list[str] | None, live: dict | None, tv: dict, *,
             breakout_vol_ratio: float = 1.5) -> tuple[list[str], dict, list[str]]:
    """Union the bot's own read with TradingView flags: TV can ADD a label, UPGRADE a range break to
    ``*_confirmed`` when its volume ratio clears the bar (e.g. a provider with no volume), and set
    the live ``*_attempt`` flags from the intraday feed -- it never removes anything the bot found.
    Returns ``(setups, live, sources)``; ``sources`` lists what TV contributed (provenance)."""
    labels = list(setups or [])
    live = dict(live or {})
    sources: list[str] = []
    vr = tv.get("vol_ratio_20")
    vol_ok = _num(vr) and vr >= breakout_vol_ratio
    for flag, base, conf in (("breakout", "breakout", "breakout_confirmed"),
                             ("breakdown", "breakdown", "breakdown_confirmed")):
        if tv.get(flag) is True:
            if base not in labels and conf not in labels:
                labels.append(base)
                sources.append(f"tv_daily:{base}")
            if vol_ok and conf not in labels:
                labels.append(conf)
                sources.append(f"tv_daily:{conf}")
    if tv.get("i_breakout_attempt") is True and not live.get("breakout_attempt"):
        live["breakout_attempt"] = True
        sources.append("tv_intraday:breakout_attempt")
    if tv.get("i_breakdown_attempt") is True and not live.get("breakdown_attempt"):
        live["breakdown_attempt"] = True
        sources.append("tv_intraday:breakdown_attempt")
    if tv.get("i_support_test") is True and "support_test_rejection" not in labels:
        labels.append("support_test_rejection")
        sources.append("tv_intraday:support_test_rejection")
    ordered = [x for x in PRIORITY if x in labels] + sorted(x for x in labels if x not in PRIORITY)
    return ordered, live, sources


def setup_sort_key(candidate, context_by_underlying: dict, inner_key: Callable) -> tuple:
    """Ranking key for ``prefer_setups``: setup score first (favorable > neutral > avoid), then the
    inner key (theta-efficiency / IV-rank / quality) breaks ties. Unknown -> 0 (neutral), so names
    without a read are never penalized. Sort descending."""
    ctx = context_by_underlying.get(candidate.underlying)
    score = getattr(ctx, "setup_score", None) if ctx is not None else None
    inner = inner_key(candidate)
    if not isinstance(inner, tuple):
        inner = (inner,)
    return (float(score if score is not None else 0), *inner)
