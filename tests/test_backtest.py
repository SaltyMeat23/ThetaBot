"""bs_delta + CSP backtest engine (deterministic win + assignment via a stubbed feed)."""
from datetime import date, timedelta

import pytest

from agentic.backtest.engine import CspBacktest
from agentic.config import EntryCriteria
from agentic.marketdata.blackscholes import bs_delta

R = 0.045


def test_bs_delta_signs_and_bounds():
    atm_put = bs_delta(10, 10, 0.1, R, 0.4, is_call=False)
    assert -0.6 < atm_put < -0.4                      # ATM put ~ -0.5
    assert bs_delta(10, 10, 0.1, R, 0.4, is_call=True) > 0.4   # ATM call ~ +0.5
    assert bs_delta(10, 6, 0.1, R, 0.4, is_call=False) > -0.05  # deep OTM put ~ 0
    assert bs_delta(10, 20, 0.1, R, 0.4, is_call=False) < -0.9  # deep ITM put ~ -1


# --- wide criteria so the single provided strike is always the pick ---
CRIT = EntryCriteria(delta_min=0.01, delta_max=0.99, dte_min=30, dte_max=45,
                     min_annualized_yield=0.0, min_open_interest=0, min_volume=0,
                     max_spread_pct=1.0, exclude_earnings_days=0)


def _weekdays(a: date, b: date):
    d, out = a, []
    while d <= b:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


class StubMD:
    def __init__(self):
        # underlying: $10 everywhere except August (drops to $9 -> assignment for the Aug expiry)
        self._closes = {d: (9.0 if d[5:7] == "08" else 10.0)
                        for d in _weekdays(date(2024, 6, 1), date(2024, 9, 30))}
        self._contracts = [
            {"symbol": "P1", "strike": 9.5, "expiration": "2024-07-19", "type": "put"},
            {"symbol": "P2", "strike": 10.0, "expiration": "2024-08-16", "type": "put"},
        ]
        # P1: 0.50 then drops to 0.20 (profit target). P2: flat 0.60 (rides to assignment).
        self._series = {
            "P1": {d: (0.50 if d < "2024-06-25" else 0.20)
                   for d in _weekdays(date(2024, 6, 3), date(2024, 7, 19))},
            "P2": {d: 0.60 for d in _weekdays(date(2024, 7, 1), date(2024, 8, 16))},
        }

    async def get_underlying_bars(self, symbol, lookback_days=730):
        return [{"date": d, "c": c} for d, c in sorted(self._closes.items())]

    async def list_option_contracts(self, symbol, exp_gte, exp_lte, opt_type="put"):
        return self._contracts

    async def get_option_bars(self, symbol, start, end):
        s = self._series.get(symbol, {})
        return [{"date": d, "c": c} for d, c in sorted(s.items()) if start <= d <= end]


@pytest.mark.asyncio
async def test_backtest_win_and_assignment():
    bt = CspBacktest(StubMD(), CRIT, dte_close=0, slippage_pct=0.05)
    r = await bt.run("X", date(2024, 6, 1), date(2024, 9, 30))
    assert r.n_trades == 2
    reasons = {t["exit_reason"] for t in r.trades}
    assert reasons == {"profit_target", "assigned"}

    win = next(t for t in r.trades if t["exit_reason"] == "profit_target")
    assert win["pnl"] == 26.5          # (0.50*0.95 - 0.20*1.05) * 100
    lose = next(t for t in r.trades if t["exit_reason"] == "assigned")
    assert lose["pnl"] == -43.0        # (0.60*0.95 - (10-9)) * 100

    assert r.wins == 1 and r.win_rate == 0.5 and r.assignment_rate == 0.5
    assert r.total_pnl == -16.5
    assert r.max_drawdown <= -43.0     # dropped from +26.5 to -16.5
