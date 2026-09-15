"""Weekly tactical brief builder: renders all sections, the bot's strike target, and degrades safely."""
from dataclasses import dataclass
from datetime import date, datetime, timezone

from agentic.marketdata.econ_calendar import EconEvent
from agentic.services.weekly_brief import build_weekly_brief

NOW = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)  # a Monday


@dataclass
class _Pos:
    underlying: str
    option_type: str
    strike: float
    quantity: int
    expiration: date
    status: str = "OPEN"

    def dte(self, today=None):
        return (self.expiration - date.today()).days


def _full():
    return dict(
        watchlist=["SMR", "F"],
        contexts={
            "SMR": {"price": 10.81, "sma200": 12.91, "adx": 19.0, "iv_rank": 62.0,
                    "quality_score": 55.0, "days_to_earnings": 6},
            "F": {"price": 13.8, "sma200": 12.0, "adx": 27.0, "iv_rank": 30.0},
        },
        candidates=[
            {"underlying": "SMR", "strike": 9.5, "delta": -0.24, "dte": 10, "premium": 0.30},
            {"underlying": "SMR", "strike": 9.0, "delta": -0.18, "dte": 10, "premium": 0.20},  # 2nd, ignored
        ],
        tv_by_symbol={"SMR": {"support": 8.53, "resistance": 11.37}},
        regime={"label": "elevated", "risk_off": False, "vix": 22.5, "vix_state": "elevated",
                "spy_above_sma200": True, "qqq_above_sma200": True, "spy_drawdown_20d": -0.031},
        news_by_symbol={"SMR": "NuScale signs new SMR supply deal"},
        accounts=[{
            "account_number": "1234", "account_value": 25000, "buying_power": 8000, "weekly_target": 125,
            "holdings": [{"symbol": "AAPL", "shares": 100}],
            "covered_calls": [{"underlying": "AAPL", "strike": 240, "dte": 7, "weekly_dollars": 65, "below_basis": False}],
            "cash_secured_puts": [{"underlying": "SMR", "strike": 9.5, "dte": 10, "weekly_dollars": 30}],
        }],
        econ_events=[EconEvent(date(2026, 9, 16), "FOMC rate decision", "high")],
        now=NOW,
    )


def test_all_sections_and_disclaimer():
    title, md = build_weekly_brief(**_full())
    assert "Weekly tactical brief" in title
    assert "NOT financial advice" in md
    for header in ("## Market backdrop", "## This week's catalysts", "## Watchlist prep", "## Broader portfolio"):
        assert header in md


def test_backdrop_and_catalysts():
    _t, md = build_weekly_brief(**_full())
    assert "VIX: 22.5 (elevated)" in md and "elevated" in md
    assert "FOMC rate decision (high)" in md
    assert "SMR: in ~6d" in md  # earnings within 2 weeks


def test_watchlist_shows_levels_and_bot_strike_target():
    _t, md = build_weekly_brief(**_full())
    assert "### SMR" in md
    assert "below 200-SMA" in md and "support 8.53" in md and "resistance 11.37" in md
    assert "62 (premium is rich)" in md
    assert "Bot's rule is eyeing:" in md and "$9.5 put" in md and "0.24 delta" in md
    assert "mechanical screen, not a recommendation" in md
    # earnings (6d) lands before the 10d expiry -> the stronger inside-the-contract flag
    assert "Earnings INSIDE the contract" in md and "~6d" in md


def test_portfolio_section():
    _t, md = build_weekly_brief(**_full())
    assert "Account 1234" in md and "AAPL x100" in md
    assert "CC idea: AAPL $240C" in md and "CSP idea: SMR $9.5P" in md


def test_ai_analysis_rendered_when_present():
    _t, md = build_weekly_brief(**_full(), ai_analysis="VIX elevated; SMR sits above support with rich IV.")
    assert "## Tactical read" in md and "VIX elevated; SMR sits above support" in md and "not advice" in md


def test_ai_analysis_absent_when_none():
    _t, md = build_weekly_brief(**_full())  # ai_analysis defaults to None
    assert "## Tactical read" not in md


def test_expected_move_cushion_renders_with_iv():
    args = _full()
    args["candidates"][0]["iv"] = 0.60  # give the top SMR candidate an IV so the move is computable
    _t, md = build_weekly_brief(**args)
    assert "**Cushion:**" in md and "expected move OTM" in md and "1-sigma" in md


def test_management_section_and_assignment_capacity():
    from datetime import timedelta
    soon = date.today() + timedelta(days=8)
    positions = [
        _Pos("SMR", "put", 12.0, 1, soon),   # ctx price 10.81 -> ITM, in roll window
        _Pos("F", "put", 9.0, 2, date.today() + timedelta(days=40)),
    ]
    _t, md = build_weekly_brief(**_full(), open_positions=positions, total_buying_power=10000.0)
    assert "## Position management" in md
    assert "Assignment capacity" in md and "coverage" in md    # 12*100 + 9*100*2 = 3000 collateral
    assert "SMR $12P" in md and "ITM" in md and "roll window" in md
    assert "F $9P" in md


def test_management_empty_when_no_positions():
    _t, md = build_weekly_brief(**_full())  # open_positions defaults to None
    assert "## Position management" in md and "No open short options" in md


def test_degrades_gracefully():
    _t, md = build_weekly_brief(
        watchlist=["F"], contexts={}, candidates=[], tv_by_symbol={}, regime=None,
        news_by_symbol={}, accounts=[{"account_number": "9", "error": "read failed"}],
        econ_events=[], now=NOW)
    assert "Regime data unavailable" in md
    assert "None in the curated calendar" in md
    assert "none within ~2 weeks" in md
    assert "### F" in md and "200-SMA n/a" in md
    assert "unavailable (read failed)" in md
