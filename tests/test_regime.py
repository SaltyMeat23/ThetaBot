"""Market-regime module: drawdown, calm/elevated/risk_off labelling, systemic vs idiosyncratic."""
from agentic.config import RegimeConfig
from agentic.entry.regime import (
    MarketRegime,
    build_market_regime,
    classify_move,
    drawdown,
)

CFG = RegimeConfig()


def _bars(closes):
    return [{"c": c} for c in closes]


def test_drawdown_at_new_high_is_zero():
    assert drawdown([100 + i for i in range(30)], 20) == 0.0


def test_drawdown_off_peak():
    closes = [100] * 20 + [90]  # last 20 window peaks at 100, ends at 90
    assert drawdown(closes, 20) == -0.1


def test_drawdown_insufficient_data():
    assert drawdown([100, 101], 20) is None


def test_calm_regime():
    closes = [100 + 0.05 * i for i in range(60)]  # steady grind up, low vol, new highs
    reg = build_market_regime(_bars(closes), _bars(closes), CFG)
    assert reg.label == "calm"
    assert reg.risk_off is False
    assert reg.spy_drawdown_20d == 0.0


def test_elevated_regime_via_drawdown():
    closes = [100] * 30 + [100 - 0.3 * k for k in range(1, 21)]  # ~ -6% off peak
    reg = build_market_regime(_bars(closes), _bars(closes), CFG)
    assert reg.label == "elevated"
    assert reg.risk_off is False


def test_risk_off_regime_via_drawdown():
    closes = [100] * 30 + [100 - 0.7 * k for k in range(1, 21)]  # ~ -13% off peak
    reg = build_market_regime(_bars(closes), _bars(closes), CFG)
    assert reg.label == "risk_off"
    assert reg.risk_off is True


def test_unknown_regime_on_empty():
    reg = build_market_regime([], [], CFG)
    assert reg.label == "unknown"
    assert reg.risk_off is None


def test_classify_systemic():
    # stock down AND market down -> the whole market is falling
    reg = MarketRegime(spy_drawdown_20d=-0.06)
    assert classify_move(-0.08, reg, CFG) == "systemic"


def test_classify_idiosyncratic():
    # stock down but market is holding -> just this stock
    reg = MarketRegime(spy_drawdown_20d=-0.01)
    assert classify_move(-0.08, reg, CFG) == "idiosyncratic"


def test_classify_neutral_when_stock_flat():
    reg = MarketRegime(spy_drawdown_20d=-0.06)
    assert classify_move(-0.01, reg, CFG) == "neutral"


def test_classify_unknown_when_no_stock_data():
    assert classify_move(None, MarketRegime(), CFG) == "unknown"


# --- VIX regime signal -----------------------------------------------------------------------

def _calm_bars():
    # gently rising SPY -> low realized vol, no drawdown -> proxy says "calm"
    return _bars([100 + i * 0.1 for i in range(220)])


def test_vix_state_bands():
    calm = _bars([100 + i * 0.1 for i in range(220)])
    assert build_market_regime(calm, calm, CFG, vix=14.0).vix_state == "calm"
    assert build_market_regime(calm, calm, CFG, vix=24.0).vix_state == "elevated"
    assert build_market_regime(calm, calm, CFG, vix=33.0).vix_state == "stress"
    assert build_market_regime(calm, calm, CFG, vix=None).vix_state is None


def test_vix_stress_forces_risk_off_even_in_a_calm_tape():
    calm = _calm_bars()
    reg = build_market_regime(calm, calm, CFG, vix=33.0)   # tape calm, but VIX screaming
    assert reg.label == "risk_off" and reg.risk_off is True
    assert reg.vix == 33.0


def test_vix_elevated_labels_elevated():
    calm = _calm_bars()
    reg = build_market_regime(calm, calm, CFG, vix=22.0)
    assert reg.label == "elevated" and reg.risk_off is False


def test_no_vix_falls_back_to_realized_vol_proxy():
    # unchanged behavior when VIX is absent: a calm tape stays calm
    calm = _calm_bars()
    reg = build_market_regime(calm, calm, CFG, vix=None)
    assert reg.vix is None and reg.label == "calm"
