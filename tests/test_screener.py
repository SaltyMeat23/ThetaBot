"""CSP screener: criteria filtering, math, liquidity hygiene, ranking."""
from datetime import date, timedelta
from types import SimpleNamespace

from agentic.config import EntryCriteria
from agentic.entry.screener import EntryCandidate, passes_candidate_gates, screen_candidates
from agentic.marketdata.quote import OptionContractQuote

TODAY = date(2026, 6, 28)

CRIT = EntryCriteria(
    delta_min=0.20, delta_max=0.30, dte_min=30, dte_max=45,
    min_annualized_yield=0.20, min_open_interest=100, min_volume=10,
    max_spread_pct=0.15, exclude_earnings_days=0,
)


def _put(strike, dte, delta, bid, ask, oi=500, vol=50, opt_type="put", theta=None, gamma=None):
    return OptionContractQuote(
        occ_symbol=f"X{strike}", underlying="X", option_id=None, option_type=opt_type,
        strike=strike, expiration=TODAY + timedelta(days=dte), bid=bid, ask=ask,
        mark=round((bid + ask) / 2, 4) if bid and ask else None,
        delta=delta, iv=0.45, theta=theta, gamma=gamma, open_interest=oi, volume=vol,
    )


def test_theta_greeks_captured_and_drive_ranking():
    """Greeks flow through, and theta-efficiency (|theta|/strike) ranks — not just yield."""
    out = screen_candidates(
        "X", [_put(100, 35, -0.25, 2.40, 2.60, theta=-0.08, gamma=0.015)], CRIT, today=TODAY)
    c = out[0]
    assert c.theta == -0.08 and c.gamma == 0.015
    # Real theta drives efficiency: |theta|/strike = 0.08/100 = 0.0008 (the OPRA rate, not a proxy).
    assert abs(c.theta_efficiency - 0.0008) < 1e-6

    # With real greeks present, a higher-theta contract outranks a fatter-premium one.
    chain = [
        _put(50, 35, -0.28, 1.60, 1.70, theta=-0.010),   # eff 0.010/50 = 0.00020
        _put(100, 35, -0.25, 2.40, 2.60, theta=-0.090),  # eff 0.090/100 = 0.00090 -> first
    ]
    ranked = screen_candidates("X", chain, CRIT, today=TODAY)
    assert ranked[0].strike == 100 and ranked[0].theta_efficiency > ranked[1].theta_efficiency


def test_screen_filters_and_ranks():
    chain = [
        _put(100, 35, -0.25, 2.40, 2.60),                 # GOOD: ror 2.5%, ann ~26%
        _put(50, 35, -0.28, 1.60, 1.70, oi=1000, vol=100),  # GOOD: ann ~34% -> ranks first
        _put(100, 35, -0.45, 2.40, 2.60),                 # delta too high
        _put(100, 35, -0.10, 2.40, 2.60),                 # delta too low
        _put(100, 10, -0.25, 2.40, 2.60),                 # dte too short
        _put(100, 35, -0.25, 2.00, 3.00),                 # spread 40% > 15%
        _put(100, 35, -0.25, 0.40, 0.50),                 # yield too low (~4.7%)
        _put(100, 35, -0.25, 0.0, 0.50),                  # penny / no bid
        _put(100, 35, -0.25, 2.40, 2.60, oi=50),          # OI below floor
        _put(100, 35, -0.25, 2.40, 2.60, opt_type="call"),  # not a put (CSP only)
    ]
    out = screen_candidates("X", chain, CRIT, today=TODAY)
    assert len(out) == 2
    # ranked by annualized yield desc -> strike 50 first
    assert out[0].strike == 50 and out[1].strike == 100
    assert out[0].score >= out[1].score

    c = out[1]  # the strike-100 GOOD one
    assert c.premium == 2.5
    assert c.ror == 2.5
    assert round(c.annualized_ror, 1) == 26.1
    assert c.max_risk == 10000.0
    assert c.break_even == 97.5


def test_zero_volume_and_oi_dropped():
    chain = [_put(100, 35, -0.25, 2.40, 2.60, oi=0, vol=0)]
    assert screen_candidates("X", chain, CRIT, today=TODAY) == []


def test_unknown_liquidity_not_failed():
    # OI/volume None (Alpaca often omits OI) -> floors skipped, candidate still considered.
    c = _put(100, 35, -0.25, 2.40, 2.60, oi=None, vol=None)
    out = screen_candidates("X", [c], CRIT, today=TODAY)
    assert len(out) == 1


def test_support_ceiling_drops_strikes_above_support():
    # Two otherwise-good puts; support=95 must drop the strike-100 put, keep the strike-90 put.
    chain = [
        _put(100, 35, -0.25, 2.40, 2.60),                 # strike above support -> dropped
        _put(90, 35, -0.28, 2.20, 2.35, oi=1000, vol=100),  # strike below support -> kept
    ]
    out = screen_candidates("X", chain, CRIT, today=TODAY, support_ceiling=95.0)
    assert [c.strike for c in out] == [90.0]


def test_support_ceiling_none_is_no_gate():
    chain = [_put(100, 35, -0.25, 2.40, 2.60), _put(90, 35, -0.28, 2.20, 2.35)]
    out = screen_candidates("X", chain, CRIT, today=TODAY, support_ceiling=None)
    assert {c.strike for c in out} == {100.0, 90.0}


# --- context-aware candidate gates: expected-move cushion + VRP floor ----------------------------

def _egcand(strike=90, iv=0.60, dte=11):
    return EntryCandidate(
        underlying="X", occ_symbol="X", option_id=None, strike=strike,
        expiration=TODAY + timedelta(days=dte), dte=dte, delta=-0.25, iv=iv, premium=0.3,
        ror=1.0, annualized_ror=30.0, max_risk=strike * 100, break_even=strike - 0.3,
        open_interest=500, volume=50, score=1.0)


def test_vrp_floor_gate():
    ctx = SimpleNamespace(realized_vol=0.66, price=100.0)
    crit = EntryCriteria(min_iv_rv_ratio=1.1)
    r = passes_candidate_gates(_egcand(iv=0.60), ctx, crit)   # 0.60/0.66 = 0.91 < 1.1
    assert r is not None and "iv/rv" in r
    assert passes_candidate_gates(_egcand(iv=0.80), ctx, crit) is None   # 0.80/0.66 = 1.21 passes


def test_expected_move_cushion_gate():
    ctx = SimpleNamespace(realized_vol=0.66, price=100.0)
    crit = EntryCriteria(min_strike_expected_moves=1.0)  # EM ~ 10.4 at iv 0.60, 11 DTE
    assert passes_candidate_gates(_egcand(strike=95), ctx, crit) is not None   # 0.48 EM -> thin
    assert passes_candidate_gates(_egcand(strike=80), ctx, crit) is None       # 1.9 EM -> passes


def test_candidate_gates_fail_open_on_missing_data():
    crit = EntryCriteria(min_iv_rv_ratio=1.1, min_strike_expected_moves=1.0)
    assert passes_candidate_gates(
        _egcand(), SimpleNamespace(realized_vol=None, price=None), crit) is None
    assert passes_candidate_gates(_egcand(), SimpleNamespace(realized_vol=0.66, price=100.0),
                                  EntryCriteria()) is None   # both gates off -> passes
