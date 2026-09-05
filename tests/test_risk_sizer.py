"""RiskSizer: per-name cap, budget cap, concurrency, one-per-underlying, skip-held."""
from datetime import date, timedelta

from agentic.config import EntrySizing
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import Position
from agentic.entry.risk import RiskSizer
from agentic.entry.screener import EntryCandidate

EXP = date(2026, 6, 28) + timedelta(days=35)

SIZING = EntrySizing(
    max_position_size_pct=0.10, max_concurrent_positions=5,
    total_bp_utilization_target=0.50, buying_power_reserve_pct=0.10,
)


def _cand(underlying, strike):
    return EntryCandidate(
        underlying=underlying, occ_symbol=f"{underlying}{strike}", option_id=None,
        strike=strike, expiration=EXP, dte=35, delta=-0.25, iv=0.4, premium=1.5,
        ror=2.0, annualized_ror=20.0, max_risk=strike * 100, break_even=strike - 1.5,
        open_interest=500, volume=50, score=20.0,
    )


def _short_put(underlying, strike, qty=1):
    return Position(
        occ_symbol=f"{underlying}{strike}p", underlying=underlying, option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=qty,
        strike=strike, expiration=EXP, credit_received=1.0, status=PositionStatus.OPEN,
    )


def test_per_name_cap_and_budget():
    sizer = RiskSizer(SIZING)
    out = sizer.approve(
        [_cand("A", 50), _cand("B", 30), _cand("C", 250)],
        buying_power=100_000, account_value=100_000, open_positions=[],
    )
    by_name = {a.candidate.underlying: a.contracts for a in out}
    assert by_name["A"] == 2          # per-name cap 10k / (50*100) = 2
    assert by_name["B"] == 3          # 10k / (30*100) = 3
    assert "C" not in by_name         # 250 strike: 10k / 25k = 0 -> skipped


def test_one_per_underlying():
    sizer = RiskSizer(SIZING)
    out = sizer.approve(
        [_cand("A", 50), _cand("A", 40)],
        buying_power=100_000, account_value=100_000, open_positions=[],
    )
    assert len(out) == 1 and out[0].candidate.underlying == "A"


def test_skip_held_names():
    sizer = RiskSizer(SIZING)
    out = sizer.approve(
        [_cand("A", 50), _cand("B", 30)],
        buying_power=100_000, account_value=100_000,
        open_positions=[_short_put("A", 55)],
    )
    assert {a.candidate.underlying for a in out} == {"B"}


def test_concurrency_slots_respected():
    sizing = EntrySizing(
        max_position_size_pct=0.10, max_concurrent_positions=1,
        total_bp_utilization_target=0.50, buying_power_reserve_pct=0.10,
    )
    # already hold 1 CSP -> 0 slots left
    out = RiskSizer(sizing).approve(
        [_cand("B", 30)],
        buying_power=100_000, account_value=100_000,
        open_positions=[_short_put("Z", 40)],
    )
    assert out == []


def test_budget_ceiling():
    # total_bp_utilization_target tiny -> run budget below one contract
    sizing = EntrySizing(
        max_position_size_pct=0.50, max_concurrent_positions=5,
        total_bp_utilization_target=0.01, buying_power_reserve_pct=0.10,
    )
    out = RiskSizer(sizing).approve(
        [_cand("A", 50)],  # needs 5000 collateral; budget = 100k*0.01 = 1000
        buying_power=100_000, account_value=100_000, open_positions=[],
    )
    assert out == []


def test_evaluate_reports_rejection_reasons():
    sizer = RiskSizer(SIZING)
    res = sizer.evaluate(
        [_cand("A", 50), _cand("B", 30), _cand("C", 250)],
        buying_power=100_000, account_value=100_000, open_positions=[],
    )
    # A and B size; C (250 strike) is too big for the 10k per-name cap.
    assert {a.candidate.underlying for a in res.approved} == {"A", "B"}
    rej = {c.underlying: reason for c, reason in res.rejected}
    assert "C" in rej and "too small to size" in rej["C"]
    # approve() stays a thin wrapper over evaluate().approved
    assert sizer.approve(
        [_cand("A", 50)], buying_power=100_000, account_value=100_000, open_positions=[],
    )[0].candidate.underlying == "A"


def test_evaluate_reasons_held_and_concurrency():
    res = RiskSizer(SIZING).evaluate(
        [_cand("A", 50), _cand("A", 40)],  # second A is a dup name
        buying_power=100_000, account_value=100_000,
        open_positions=[_short_put("B", 55)],  # B already held (not a candidate here)
    )
    reasons = [reason for _c, reason in res.rejected]
    assert any("already approved this scan" in r for r in reasons)


def test_evaluate_reason_held_name():
    res = RiskSizer(SIZING).evaluate(
        [_cand("A", 50)], buying_power=100_000, account_value=100_000,
        open_positions=[_short_put("A", 55)],
    )
    assert res.approved == []
    assert any("already holding A" in reason for _c, reason in res.rejected)


# --- scale-invariant sizing (target_positions + liquidity cap) -----------------------------------

SCALED = EntrySizing(
    max_position_size_pct=0.5, max_concurrent_positions=30,
    total_bp_utilization_target=0.8, buying_power_reserve_pct=0.1, target_positions=20,
)


def test_target_positions_scales_per_name_with_capital():
    """The SAME config sizes correctly at $5k and $500k — per-name shrinks as capital grows."""
    cand = _cand("A", 8)  # $800 collateral / contract, OI 500
    small = RiskSizer(SCALED).approve([cand], buying_power=5_000, account_value=5_000,
                                      open_positions=[])
    assert small and small[0].contracts == 1     # tiny account: 1 contract (backstop allows it)
    big = RiskSizer(SCALED).approve([cand], buying_power=500_000, account_value=500_000,
                                    open_positions=[])
    # per-name budget = 500k*0.8/20 = 20k -> 20k/800 = 25 contracts (only 4% of the account)
    assert big and big[0].contracts == 25


def test_backstop_caps_small_account_concentration():
    # A single contract that would exceed the 50% backstop is rejected even on a small account.
    huge = _cand("A", 40)  # $4,000 collateral > 0.5 * $5,000 backstop
    out = RiskSizer(SCALED).approve([huge], buying_power=5_000, account_value=5_000,
                                    open_positions=[])
    assert out == []


def test_liquidity_cap_limits_large_account():
    sizing = SCALED.model_copy(update={"max_pct_of_oi": 0.10})
    thin = _cand("A", 8)
    thin.open_interest = 100          # liquidity cap = floor(100 * 0.10) = 10 contracts
    big = RiskSizer(sizing).approve([thin], buying_power=500_000, account_value=500_000,
                                    open_positions=[])
    assert big and big[0].contracts == 10   # capped by OI (10), not the 25-contract budget


def test_liquidity_cap_failopen_on_unknown_oi():
    sizing = SCALED.model_copy(update={"max_pct_of_oi": 0.10})
    unknown = _cand("A", 8)
    unknown.open_interest = None      # unknown OI -> no liquidity cap (fail-open)
    big = RiskSizer(sizing).approve([unknown], buying_power=500_000, account_value=500_000,
                                    open_positions=[])
    assert big and big[0].contracts == 25   # falls back to the budget-driven 25
