"""Loss circuit breaker: trips on windowed realized loss / loss streak; safe otherwise; opt-out."""
from datetime import datetime, timezone

from agentic.config import RiskConfig
from agentic.services.risk_breaker import evaluate_risk_breaker

NOW = datetime(2026, 9, 4, tzinfo=timezone.utc)


class _Journal:
    """Fake journal: realized_since returns a preset (sum,count); resolved_pnls returns newest-first."""
    def __init__(self, window_sum=0.0, pnls=None):
        self._sum = window_sum
        self._pnls = pnls or []
    def realized_since(self, since_iso):
        return round(self._sum, 2), len(self._pnls)
    def resolved_pnls(self, limit=50):
        return self._pnls[:limit]


def test_no_trip_when_profitable():
    j = _Journal(window_sum=105.0, pnls=[27, 14, 8, 11])   # all wins, big realized gain
    s = evaluate_risk_breaker(j, RiskConfig(), account_value=6700, now=NOW)
    assert s["tripped"] is False and s["reason"] is None
    assert s["consecutive_losses"] == 0


def test_trips_on_windowed_realized_loss():
    # -$800 realized over the window on a $6,700 account = -11.9% <= -10% limit -> trip
    j = _Journal(window_sum=-800.0, pnls=[-800])
    s = evaluate_risk_breaker(j, RiskConfig(max_consecutive_losses=0), account_value=6700, now=NOW)
    assert s["tripped"] is True and "realized" in s["reason"]
    assert s["loss_limit"] == round(-0.10 * 6700, 2)


def test_does_not_trip_below_loss_limit():
    # -$300 on $6,700 = -4.5%, under the 10% limit -> no trip
    j = _Journal(window_sum=-300.0, pnls=[-300])
    s = evaluate_risk_breaker(j, RiskConfig(max_consecutive_losses=0), account_value=6700, now=NOW)
    assert s["tripped"] is False


def test_trips_on_consecutive_losses():
    # 4 straight realized losers -> trip on the streak (small dollars, under the % limit)
    j = _Journal(window_sum=-40.0, pnls=[-8, -12, -5, -15, 20, 11])  # leading run of 4 negatives
    s = evaluate_risk_breaker(j, RiskConfig(max_realized_loss_pct=0.0), account_value=6700, now=NOW)
    assert s["consecutive_losses"] == 4
    assert s["tripped"] is True and "consecutive" in s["reason"]


def test_streak_broken_by_a_win():
    j = _Journal(window_sum=10.0, pnls=[-8, -12, 20, -5, -15])  # only 2 leading losers
    s = evaluate_risk_breaker(j, RiskConfig(max_realized_loss_pct=0.0), account_value=6700, now=NOW)
    assert s["consecutive_losses"] == 2 and s["tripped"] is False


def test_disabled_never_trips():
    j = _Journal(window_sum=-5000.0, pnls=[-100, -100, -100, -100, -100])  # catastrophic
    s = evaluate_risk_breaker(j, RiskConfig(loss_breaker_enabled=False), account_value=6700, now=NOW)
    assert s["enabled"] is False and s["tripped"] is False


def test_unknown_account_value_skips_pct_check_but_streak_still_works():
    j = _Journal(window_sum=-800.0, pnls=[-8, -12, -5, -15])
    s = evaluate_risk_breaker(j, RiskConfig(), account_value=None, now=NOW)
    # no account value -> can't do the % check, but the streak trigger still protects
    assert s["loss_limit"] is None
    assert s["tripped"] is True and "consecutive" in s["reason"]
