"""Loss circuit breaker: trips on windowed realized loss / loss streak; safe otherwise; opt-out."""
from types import SimpleNamespace
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


# --- sector / correlation cap ----------------------------------------------------------------

from agentic.services.risk_breaker import apply_sector_cap


def _appr(underlying, collateral):
    return SimpleNamespace(candidate=SimpleNamespace(underlying=underlying), collateral=collateral)


def _open_put(underlying, strike, qty):
    return SimpleNamespace(option_type=SimpleNamespace(value="put"),
                           underlying=underlying, strike=strike, quantity=qty)


_ACCT = 10_000.0


def test_sector_cap_off_keeps_all():
    kept, skipped = apply_sector_cap([_appr("SMR", 5000), _appr("CRWV", 6000)], [], RiskConfig(), _ACCT)
    assert len(kept) == 2 and skipped == []           # max_pct_per_sector None -> fail-open


def test_sector_cap_drops_overflow_in_same_sector():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "OKLO": "nuclear"})
    # cap = 3000; first nuclear (2000) fits, second (2000) would make 4000 > 3000 -> dropped
    kept, skipped = apply_sector_cap([_appr("SMR", 2000), _appr("OKLO", 2000)], [], cfg, _ACCT)
    assert [e.candidate.underlying for e in kept] == ["SMR"]
    assert len(skipped) == 1 and "nuclear" in skipped[0][1]


def test_sector_cap_allows_across_sectors():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "SOFI": "fintech"})
    kept, _ = apply_sector_cap([_appr("SMR", 2500), _appr("SOFI", 2500)], [], cfg, _ACCT)
    assert len(kept) == 2                              # different sectors, each under the cap


def test_sector_cap_counts_existing_open_puts():
    cfg = RiskConfig(max_pct_per_sector=0.30, sector_map={"SMR": "nuclear", "OKLO": "nuclear"})
    opens = [_open_put("SMR", 25.0, 1)]                # 25*100*1 = 2500 already in 'nuclear'
    kept, skipped = apply_sector_cap([_appr("OKLO", 1000)], opens, cfg, _ACCT)  # 2500+1000 > 3000
    assert kept == [] and len(skipped) == 1


def test_sector_cap_unmapped_names_are_singletons():
    # no sector_map -> each name its own sector -> both fit (2000 < 3000)
    kept, _ = apply_sector_cap([_appr("AAA", 2000), _appr("BBB", 2000)], [],
                               RiskConfig(max_pct_per_sector=0.30), _ACCT)
    assert len(kept) == 2
