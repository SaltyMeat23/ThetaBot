"""Pure indicator + IV-Rank functions."""
from agentic.entry import indicators as ind


def test_sma():
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1], 2) is None


def test_rsi_bounds_and_guard():
    up = list(range(1, 20))          # strictly increasing -> no losses -> 100
    assert ind.rsi(up, 14) == 100.0
    down = list(range(20, 1, -1))    # strictly decreasing -> no gains -> 0
    assert ind.rsi(down, 14) == 0.0
    assert ind.rsi([1, 2, 3], 14) is None  # not enough data


def test_realized_vol():
    assert ind.realized_vol([100.0] * 25, 20) == 0.0   # flat -> zero vol
    assert ind.realized_vol([1, 2], 20) is None


def test_atr():
    highs = [2.0] * 16
    lows = [1.0] * 16
    closes = [1.5] * 16
    assert ind.atr(highs, lows, closes, 14) == 1.0
    assert ind.atr([1, 2], [1, 2], [1, 2], 14) is None


def test_iv_rank():
    assert ind.iv_rank(0.5, [0.2, 0.8], min_days=2) == 50.0
    assert ind.iv_rank(0.8, [0.2, 0.8], min_days=2) == 100.0
    assert ind.iv_rank(0.5, [0.2], min_days=2) is None      # history too short
    assert ind.iv_rank(None, [0.2, 0.8], min_days=2) is None  # current unknown
    assert ind.iv_rank(0.5, [0.4, 0.4, 0.4], min_days=2) is None  # flat history (max==min)
