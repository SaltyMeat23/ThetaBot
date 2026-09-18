"""Per-underlying context: technicals + IV Rank, built once per scan and used to gate entries
and to label journal rows. Pure given its inputs (bars + IV history)."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import EntryCriteria
from . import indicators
from .regime import drawdown as _drawdown


@dataclass
class UnderlyingContext:
    symbol: str
    price: float | None = None
    sma50: float | None = None
    sma200: float | None = None
    above_sma200: bool | None = None
    rsi: float | None = None
    realized_vol: float | None = None
    atr: float | None = None
    iv_rank: float | None = None
    drawdown_20d: float | None = None      # off the 20-day high (for systemic-vs-idiosyncratic)
    days_to_earnings: int | None = None    # None = unknown / no earnings source
    # TradingView-sourced features (overlaid post-build by the scanner; None when no fresh alert).
    adx: float | None = None               # daily trend strength (ADX 14); high = strong trend
    bb_percent_b: float | None = None      # Bollinger %B: 0 = at lower band, 100 = at upper band
    recent_news_count: int | None = None   # # of recent headlines for the name (advisory context)
    # Company quality/growth score (0-100), overlaid post-build by the scanner from the company-data
    # provider. None = no company data / gate off. See scoring/quality.py.
    quality_score: float | None = None
    # Technical-setup read (entry/setups.py), overlaid by the scanner from the daily bars. Labels
    # are journaled with every entry (so the flywheel learns which patterns have edge), feed the
    # opt-in avoid_setups/require_setups gates and the prefer_setups tilt, and reach the AI
    # reviewer via as_dict(). None = no read (too little history / detection off) -> gates skip.
    setups: list[str] | None = None            # ACTIVE composite labels (PRIORITY-ordered)
    primary_setup: str | None = None
    setup_bias: str | None = None              # favorable | avoid | mixed | none
    setup_score: int | None = None
    setups_fired_now: list[str] | None = None  # labels true on the last COMPLETED bar
    bb_width_pct: float | None = None
    bb_squeeze: bool | None = None
    ttm_squeeze: bool | None = None
    donchian_high_20: float | None = None
    donchian_low_20: float | None = None
    vol_ratio_20: float | None = None
    support_ref: float | None = None           # the level used ("tv" if fresh, else Donchian low)
    support_source: str | None = None
    dist_to_support_pct: float | None = None
    resistance_ref: float | None = None
    dist_to_resistance_pct: float | None = None
    live_breakout_attempt: bool | None = None  # today's PARTIAL bar breaking the range right now
    live_breakdown_attempt: bool | None = None
    # TradingView real-time layer (setups.parse_tv_setups / merge_tv): fresh flags from the Daily +
    # intraday exporters, merged (union) into the fields above; provenance in setup_sources.
    tv_setups: dict | None = None
    tv_bar_age_seconds: float | None = None
    setup_sources: list[str] | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def build_context(
    symbol: str,
    bars: list[dict],
    atm_iv: float | None,
    iv_history: list[float],
    criteria: EntryCriteria,
) -> UnderlyingContext:
    closes = [b["c"] for b in bars if b.get("c") is not None]
    highs = [b["h"] for b in bars if b.get("h") is not None]
    lows = [b["l"] for b in bars if b.get("l") is not None]
    price = closes[-1] if closes else None
    sma200 = indicators.sma(closes, 200)
    above = (price > sma200) if (price is not None and sma200 is not None) else None
    return UnderlyingContext(
        symbol=symbol.upper(),
        price=price,
        sma50=indicators.sma(closes, 50),
        sma200=sma200,
        above_sma200=above,
        rsi=indicators.rsi(closes, 14),
        realized_vol=indicators.realized_vol(closes, 20),
        atr=indicators.atr(highs, lows, closes, 14),
        iv_rank=indicators.iv_rank(atm_iv, iv_history, criteria.iv_rank_min_history_days),
        drawdown_20d=_drawdown(closes, 20),
        # Bot-computed Bollinger %B (same 0-100 definition as the TradingView field; a fresh TV
        # value still overwrites it). Makes the opt-in min_bb_percent_b gate live without TV.
        bb_percent_b=indicators.bb_percent_b(closes),
    )


def passes_underlying_gates(ctx: UnderlyingContext, criteria: EntryCriteria) -> str | None:
    """Return None if the underlying passes all *configured + available* gates, else a short
    reason string. Gates are skipped when their data is unavailable (never block on unknown)."""
    if criteria.min_iv_rank is not None and ctx.iv_rank is not None:
        if ctx.iv_rank < criteria.min_iv_rank:
            return f"iv_rank {ctx.iv_rank} < {criteria.min_iv_rank}"
    if criteria.rsi_min is not None and ctx.rsi is not None and ctx.rsi < criteria.rsi_min:
        return f"rsi {ctx.rsi} < {criteria.rsi_min}"
    if criteria.rsi_max is not None and ctx.rsi is not None and ctx.rsi > criteria.rsi_max:
        return f"rsi {ctx.rsi} > {criteria.rsi_max}"
    if criteria.require_above_sma200 and ctx.above_sma200 is False:
        return "below_sma200 (downtrend)"
    if (criteria.max_pct_below_sma200 is not None and ctx.price is not None
            and ctx.sma200 is not None and ctx.sma200 > 0):
        pct_below = (ctx.sma200 - ctx.price) / ctx.sma200
        if pct_below > criteria.max_pct_below_sma200:
            return (f"{pct_below * 100:.1f}% below 200-SMA > "
                    f"{criteria.max_pct_below_sma200 * 100:.0f}% cap (broken downtrend)")
    if criteria.min_adx is not None and ctx.adx is not None and ctx.adx < criteria.min_adx:
        return f"adx {ctx.adx:.1f} < {criteria.min_adx} (weak/choppy trend)"
    if (criteria.min_bb_percent_b is not None and ctx.bb_percent_b is not None
            and ctx.bb_percent_b < criteria.min_bb_percent_b):
        return f"bb%b {ctx.bb_percent_b:.1f} < {criteria.min_bb_percent_b} (price at lower band)"
    if (criteria.min_quality_score is not None and ctx.quality_score is not None
            and ctx.quality_score < criteria.min_quality_score):
        return f"quality {ctx.quality_score:.0f} < {criteria.min_quality_score:.0f} (low quality/growth)"
    # Technical-setup gates: skip a name while an AVOID pattern is active (e.g. a fresh breakdown --
    # do not sell puts into it), or require a favorable one. Both skip when there is no read.
    if criteria.avoid_setups and ctx.setups is not None:
        hit = [s for s in ctx.setups if s in criteria.avoid_setups]
        if hit:
            return f"setup {hit[0]} active (avoid_setups)"
    if criteria.require_setups and ctx.setups is not None:
        if not any(s in criteria.require_setups for s in ctx.setups):
            have = ", ".join(ctx.setups) if ctx.setups else "none"
            return f"no required setup active (have: {have})"
    return None
