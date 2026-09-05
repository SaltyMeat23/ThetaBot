"""ProfitTargetRule boundary behavior."""
from datetime import date, datetime, timedelta, timezone

from agentic.domain.enums import Direction, OptionType, Strategy
from agentic.domain.models import Position
from agentic.marketdata.quote import OptionQuote
from agentic.rules.profit_target import ProfitTargetRule

NOW = datetime(2026, 6, 14, tzinfo=timezone.utc)


def _position(credit=2.00):
    return Position(
        occ_symbol="AAPL260714C00250000",
        underlying="AAPL",
        option_type=OptionType.CALL,
        strategy=Strategy.COVERED_CALL,
        direction=Direction.SHORT,
        quantity=1,
        strike=250.0,
        expiration=date(2026, 7, 14),
        credit_received=credit,
    )


def _quote(bid, ask):
    return OptionQuote(occ_symbol="AAPL260714C00250000", bid=bid, ask=ask,
                       mark=round((bid + ask) / 2, 4), as_of=NOW)


def test_triggers_at_half_credit():
    rule = ProfitTargetRule("p50", requires_approval=False, params={"profit_pct": 0.5})
    # credit 2.00, target 1.00; ask 0.95 -> below target -> trigger
    decision = rule.evaluate(_position(2.00), _quote(0.90, 0.95), NOW)
    assert decision is not None
    assert "Profit target" in decision.reason


def test_no_trigger_above_target():
    rule = ProfitTargetRule("p50", requires_approval=False, params={"profit_pct": 0.5})
    # ask 1.05 > target 1.00 -> no trigger
    assert rule.evaluate(_position(2.00), _quote(1.00, 1.05), NOW) is None


def test_requires_valid_quote():
    rule = ProfitTargetRule("p50", requires_approval=False, params={"profit_pct": 0.5})
    assert rule.evaluate(_position(2.00), None, NOW) is None
    # bid 0 -> invalid quote
    assert rule.evaluate(_position(2.00), _quote(0.0, 0.95), NOW) is None


def test_zero_credit_never_triggers():
    rule = ProfitTargetRule("p50", requires_approval=False, params={"profit_pct": 0.5})
    assert rule.evaluate(_position(0.0), _quote(0.10, 0.15), NOW) is None
