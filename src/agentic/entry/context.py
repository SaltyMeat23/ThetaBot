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
    return None
