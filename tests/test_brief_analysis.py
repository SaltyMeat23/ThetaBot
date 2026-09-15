"""AI tactical-analysis generator for the weekly brief: fail-open + payload shape. No network."""
from datetime import date

import pytest

from agentic.ai.brief_analysis import generate_brief_analysis
from agentic.marketdata.econ_calendar import EconEvent


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    async def summarize(self, system, user, max_tokens=400):
        self.calls.append((system, user))
        return "Market elevated; SMR sits above support with rich IV. Watch FOMC Wed."


@pytest.mark.asyncio
async def test_returns_none_without_client():
    out = await generate_brief_analysis(
        None, watchlist=["SMR"], contexts={}, candidates=[], tv_by_symbol={}, regime=None, econ_events=[])
    assert out is None


@pytest.mark.asyncio
async def test_generates_and_packs_payload():
    c = _FakeClient()
    out = await generate_brief_analysis(
        c, watchlist=["SMR"],
        contexts={"SMR": {"price": 10.8, "sma200": 12.9, "adx": 19, "iv_rank": 62, "days_to_earnings": 6}},
        candidates=[{"underlying": "SMR", "strike": 9.5, "delta": -0.24, "dte": 10}],
        tv_by_symbol={"SMR": {"support": 8.53, "resistance": 11.37}},
        regime={"vix": 22.5, "vix_state": "elevated"},
        econ_events=[EconEvent(date(2026, 9, 16), "FOMC rate decision", "high")])
    assert out and "SMR" in out
    system, user = c.calls[0]
    assert "not financial advice" in system.lower()               # guardrail in the prompt
    assert "8.53" in user and "FOMC rate decision" in user and "iv_rank" in user  # levels + catalysts packed


@pytest.mark.asyncio
async def test_packs_flywheel_skips_ivrv_and_risk():
    c = _FakeClient()
    await generate_brief_analysis(
        c, watchlist=["SMR"],
        contexts={"SMR": {"price": 10.8, "sma200": 12.9, "realized_vol": 0.5, "drawdown_20d": -0.1}},
        candidates=[{"underlying": "SMR", "strike": 9.5, "delta": -0.24, "dte": 10, "iv": 0.75,
                     "annualized_ror": 40.0, "break_even": 9.2}],
        tv_by_symbol={"SMR": {"support": 8.53}},
        regime={"vix": 22.5, "spy_drawdown_20d": -0.02},
        econ_events=[],
        flywheel={"summary": {"resolved_trades": 14, "win_rate": 0.9},
                  "by_feature": {"delta": [{"bucket": "0.20-0.25", "n": 5, "win_rate": 0.8, "avg_pnl": 12}]}},
        skips=[{"symbol": "TE", "reason": "28% below 200-SMA > 20% cap"}],
        risk={"loss_breaker": {"tripped": False}, "sector_exposure": [{"sector": "nuclear", "pct_of_account": 24}]},
        accounts=[{"account_number": "1", "account_value": 8000, "holdings": [{"symbol": "SMR", "shares": 9}]}],
        ai_reviews=[{"underlying": "SMR", "recommendation": "proceed", "flags": []}])
    _sys, user = c.calls[0]
    assert "your_history" in user and "0.20-0.25" in user                 # flywheel bucket forwarded
    assert "skipped_today" in user and "28% below 200-SMA" in user        # skip reasons forwarded
    assert "iv_rv_ratio" in user and "1.5" in user                        # iv 0.75 / rv 0.5 computed
    assert "sector_exposure" in user and "nuclear" in user                # concentration forwarded
    assert "systemic_dip" in user                                         # systemic vs idiosyncratic label


@pytest.mark.asyncio
async def test_packs_cushion_earnings_and_book_risk():
    from dataclasses import dataclass
    from datetime import date as _date, timedelta

    @dataclass
    class _Pos:
        underlying: str
        option_type: str
        strike: float
        quantity: int
        expiration: _date
        status: str = "OPEN"

        def dte(self, today=None):
            return (self.expiration - _date.today()).days

    c = _FakeClient()
    await generate_brief_analysis(
        c, watchlist=["SMR"],
        contexts={"SMR": {"price": 10.8, "atr": 0.5, "days_to_earnings": 6}},
        candidates=[{"underlying": "SMR", "strike": 9.5, "delta": -0.24, "dte": 10,
                     "iv": 0.60, "premium": 0.30}],
        tv_by_symbol={"SMR": {"support": 8.5}},
        regime={"vix": 22.5}, econ_events=[],
        open_positions=[_Pos("SMR", "put", 12.0, 1, _date.today() + timedelta(days=8))],
        total_buying_power=5000.0)
    _sys, user = c.calls[0]
    assert "expected_move" in user and "strike_em" in user          # cushion metrics packed
    assert "support_below_strike_em" in user
    assert "earnings_inside_contract" in user                       # binary-event flag packed
    assert "assignment" in user and "collateral" in user            # assignment capacity packed
    assert "management" in user and "roll_window" in user           # open-position management packed
    assert "expected-move" in _sys.lower() and "ranked verdict" in _sys.lower()  # prompt guidance


@pytest.mark.asyncio
async def test_fail_open_on_client_error():
    class Boom:
        async def summarize(self, *a, **k):
            raise RuntimeError("api down")
    out = await generate_brief_analysis(
        Boom(), watchlist=["F"], contexts={}, candidates=[], tv_by_symbol={}, regime=None, econ_events=[])
    assert out is None
