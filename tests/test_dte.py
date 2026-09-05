"""DteRule boundary behavior."""
from datetime import date, datetime, timezone

from agentic.domain.enums import Direction, OptionType, Strategy
from agentic.domain.models import Position
from agentic.rules.dte import DteRule

NOW = datetime(2026, 6, 14, tzinfo=timezone.utc)  # date 2026-06-14


def _position(expiration: date):
    return Position(
        occ_symbol="MSFT_P",
        underlying="MSFT",
        option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT,
        direction=Direction.SHORT,
        quantity=1,
        strike=400.0,
        expiration=expiration,
        credit_received=3.50,
    )


def test_triggers_at_threshold():
    rule = DteRule("dte21", requires_approval=True, params={"dte_threshold": 21, "action": "alert"})
    # exactly 21 days out -> trigger (<=)
    decision = rule.evaluate(_position(date(2026, 7, 5)), None, NOW)
    assert decision is not None
    assert "DTE 21" in decision.reason


def test_no_trigger_when_far_out():
    rule = DteRule("dte21", requires_approval=True, params={"dte_threshold": 21})
    assert rule.evaluate(_position(date(2026, 8, 1)), None, NOW) is None


def test_triggers_when_inside_threshold():
    rule = DteRule("dte21", requires_approval=True, params={"dte_threshold": 21})
    assert rule.evaluate(_position(date(2026, 6, 20)), None, NOW) is not None
