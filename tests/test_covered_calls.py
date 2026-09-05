"""Covered-call screening (calls + cost-basis floor), CC sizing, per-ticker overrides."""
from datetime import date, timedelta

from agentic.config import EntryConfig, EntryCriteria, EntrySizing
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import EquityHolding, Position
from agentic.entry.risk import RiskSizer
from agentic.entry.screener import screen_candidates
from agentic.marketdata.quote import OptionContractQuote

TODAY = date(2026, 6, 30)
CRIT = EntryCriteria(
    delta_min=0.20, delta_max=0.30, dte_min=30, dte_max=45,
    min_annualized_yield=0.10, min_open_interest=100, min_volume=10,
    max_spread_pct=0.15, exclude_earnings_days=0,
)
SIZING = EntrySizing(max_concurrent_cc=10)


def _call(strike, dte, delta, bid, ask, oi=500, vol=50, opt_type="call"):
    return OptionContractQuote(
        occ_symbol=f"X{strike}{opt_type[0]}", underlying="X", option_id=None,
        option_type=opt_type, strike=strike, expiration=TODAY + timedelta(days=dte),
        bid=bid, ask=ask, mark=round((bid + ask) / 2, 4), delta=delta,
        iv=0.4, open_interest=oi, volume=vol,
    )


def test_cc_screener_calls_and_strike_floor():
    chain = [
        _call(45, 35, 0.25, 0.80, 0.90),                 # GOOD: above floor 40
        _call(38, 35, 0.25, 0.80, 0.90),                 # below cost-basis floor -> dropped
        _call(45, 35, 0.25, 0.80, 0.90, opt_type="put"),  # not a call -> dropped
    ]
    out = screen_candidates("X", chain, CRIT, today=TODAY, option_type="call", strike_floor=40.0)
    assert len(out) == 1
    assert out[0].strike == 45 and out[0].premium == 0.85


def _short_call(underlying, qty):
    return Position(
        occ_symbol=f"{underlying}call", underlying=underlying, option_type=OptionType.CALL,
        strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=qty,
        strike=45.0, expiration=TODAY + timedelta(days=35), credit_received=0.85,
        status=PositionStatus.OPEN,
    )


def _cc_cand(underlying, strike=45):
    return screen_candidates(
        underlying,
        [OptionContractQuote(
            occ_symbol=f"{underlying}{strike}c", underlying=underlying, option_id=None,
            option_type="call", strike=strike, expiration=TODAY + timedelta(days=35),
            bid=0.80, ask=0.90, mark=0.85, delta=0.25, iv=0.4, open_interest=500, volume=50,
        )],
        CRIT, today=TODAY, option_type="call", strike_floor=0.0,
    )[0]


def test_cc_sizer_by_shares():
    sizer = RiskSizer(SIZING)
    holdings = [EquityHolding(symbol="X", quantity=250, average_cost=40.0)]
    out = sizer.approve_covered_calls([_cc_cand("X")], holdings=holdings, open_positions=[])
    assert len(out) == 1 and out[0].contracts == 2          # 250 // 100


def test_cc_sizer_subtracts_existing_covers():
    sizer = RiskSizer(SIZING)
    holdings = [EquityHolding(symbol="X", quantity=250, average_cost=40.0)]
    out = sizer.approve_covered_calls(
        [_cc_cand("X")], holdings=holdings, open_positions=[_short_call("X", 1)]
    )
    assert out[0].contracts == 1                            # 2 coverable - 1 already short


def test_cc_sizer_skips_unheld_and_fully_covered():
    sizer = RiskSizer(SIZING)
    holdings = [EquityHolding(symbol="X", quantity=150, average_cost=40.0)]
    # only 1 lot coverable, already 1 short -> nothing left; Y not held at all
    out = sizer.approve_covered_calls(
        [_cc_cand("X"), _cc_cand("Y")], holdings=holdings,
        open_positions=[_short_call("X", 1)],
    )
    assert out == []


def test_per_ticker_override_merge():
    cfg = EntryConfig(per_ticker={"NVDA": {"delta_max": 0.25, "dte_min": 7}})
    merged = cfg.criteria_for("NVDA", CRIT)
    assert merged.delta_max == 0.25 and merged.dte_min == 7
    assert merged.delta_min == CRIT.delta_min      # untouched fields preserved
    # a name with no overrides returns the base unchanged
    assert cfg.criteria_for("AAPL", CRIT) is CRIT
