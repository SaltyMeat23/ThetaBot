"""Market-regime read: is the whole market falling (systemic) or is it just one stock
(idiosyncratic)? Pure functions over index-ETF daily bars, mirroring entry/indicators.py.

No I/O. Every field is None-tolerant, so a data gap degrades to "unknown" rather than an error
(same discipline as UnderlyingContext). Computed once per scan; the AI reviewer interprets it.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from ..config import RegimeConfig
from . import indicators


def drawdown(closes: list[float], n: int = 20) -> float | None:
    """Fractional drawdown of the latest close from the highest close over the last n bars.

    Negative = below the recent peak (e.g. -0.08 = 8% off the 20-day high); 0.0 = at a new high.
    """
    if len(closes) < n or n <= 0:
        return None
    window = closes[-n:]
    peak = max(window)
    if peak <= 0:
        return None
    return round((window[-1] - peak) / peak, 4)


@dataclass
class MarketRegime:
    label: str = "unknown"          # calm | elevated | risk_off | unknown
    risk_off: bool | None = None
    spy_price: float | None = None
    spy_sma50: float | None = None
    spy_sma200: float | None = None
    spy_above_sma200: bool | None = None
    qqq_above_sma200: bool | None = None
    spy_drawdown_20d: float | None = None
    spy_realized_vol: float | None = None   # annualized fraction — the VIX proxy (v1)

    def as_dict(self) -> dict:
        return asdict(self)


def _above_sma200(closes: list[float]) -> bool | None:
    price = closes[-1] if closes else None
    s200 = indicators.sma(closes, 200)
    if price is None or s200 is None:
        return None
    return price > s200


def _label(reg: "MarketRegime", cfg: RegimeConfig) -> tuple[str, bool | None]:
    rv, dd = reg.spy_realized_vol, reg.spy_drawdown_20d
    if rv is None and dd is None:
        return "unknown", None
    risk_off = (
        (rv is not None and rv >= cfg.risk_off_vol)
        or (dd is not None and dd <= -cfg.risk_off_drawdown)
    )
    if risk_off:
        return "risk_off", True
    elevated = (
        (rv is not None and rv >= cfg.elevated_vol)
        or (dd is not None and dd <= -cfg.elevated_drawdown)
    )
    return ("elevated" if elevated else "calm"), False


def build_market_regime(
    spy_bars: list[dict], qqq_bars: list[dict], cfg: RegimeConfig
) -> MarketRegime:
    """Build the market regime from SPY + QQQ daily bars ([{date,o,h,l,c,v}], oldest->newest)."""
    spy_closes = [b["c"] for b in spy_bars if b.get("c") is not None]
    qqq_closes = [b["c"] for b in qqq_bars if b.get("c") is not None]
    reg = MarketRegime(
        spy_price=(spy_closes[-1] if spy_closes else None),
        spy_sma50=indicators.sma(spy_closes, 50),
        spy_sma200=indicators.sma(spy_closes, 200),
        spy_above_sma200=_above_sma200(spy_closes),
        qqq_above_sma200=_above_sma200(qqq_closes),
        spy_drawdown_20d=drawdown(spy_closes, 20),
        spy_realized_vol=indicators.realized_vol(spy_closes, 20),
    )
    reg.label, reg.risk_off = _label(reg, cfg)
    return reg


def classify_move(
    stock_drawdown_20d: float | None, regime: MarketRegime, cfg: RegimeConfig
) -> str:
    """Is a stock's recent drop market-wide (systemic) or its own (idiosyncratic)?

    Returns 'systemic' | 'idiosyncratic' | 'neutral' | 'unknown'. This is the "free-fall vs blip"
    signal: a stock down while the market is also down = systemic; down while the market holds
    = just this name.
    """
    if stock_drawdown_20d is None:
        return "unknown"
    if stock_drawdown_20d > -cfg.stock_move_min:
        return "neutral"                        # stock isn't meaningfully down
    market_dd = regime.spy_drawdown_20d
    if market_dd is not None and market_dd <= -cfg.systemic_drawdown:
        return "systemic"                       # the whole market is falling too — free-fall
    return "idiosyncratic"                       # just this stock
