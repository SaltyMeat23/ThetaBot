"""Quality/growth score: factor math, blend/renormalization, junk floor, momentum, fail-open."""
import pytest

from agentic.marketdata.company_data import CompanyProfile
from agentic.scoring.quality import quality_breakdown, quality_growth_score


def _bars(closes):
    return [{"o": c, "h": c, "l": c, "c": c, "v": 1} for c in closes]


HIGH = CompanyProfile(
    symbol="HIGH", gross_profitability=0.45, operating_margin=0.28,
    fcf_margin=0.22, revenue_growth=0.20,
)
# Genuine junk: cash-burning, unprofitable, low-margin AND barely growing (no reinvestment story).
JUNK = CompanyProfile(
    symbol="JUNK", net_margin=-0.35, fcf_margin=-0.25,
    gross_margin=0.10, revenue_growth=-0.05,
)
# A cash-burning hypergrowth reinvestor (CRWV-like): burning + unprofitable BUT high growth + high
# gross margin -> should be SPARED the junk floor (this is the kind of name we want to find).
REINVESTOR = CompanyProfile(
    symbol="REINV", net_margin=-0.24, fcf_margin=-1.4,
    gross_margin=0.70, revenue_growth=0.40,
)


def test_high_quality_beats_junk():
    up = _bars([100 + i * 0.3 for i in range(300)])     # steady uptrend
    hi = quality_growth_score(HIGH, up, below_sma200=False)
    jk = quality_growth_score(JUNK, up, below_sma200=False)
    assert hi is not None and jk is not None
    assert hi > 70
    assert jk < hi
    assert jk <= 30  # junk floor: cash-burning + unprofitable + low-margin + no growth, capped low


def test_junk_floor_applies_even_with_strong_momentum():
    parabola = _bars([100 * (1.01 ** i) for i in range(300)])  # +200%+ momentum
    assert quality_growth_score(JUNK, parabola, below_sma200=False) <= 30


def test_reinvestor_spared_from_junk_floor():
    # a high-growth, high-gross-margin cash-burner is NOT floored (the burn funds real expansion)
    parabola = _bars([100 * (1.005 ** i) for i in range(300)])
    assert quality_growth_score(REINVESTOR, parabola, below_sma200=False) > 30


def test_insider_buys_add_bonus():
    base = CompanyProfile(symbol="B", gross_margin=0.40, revenue_growth=0.10)
    with_buys = CompanyProfile(symbol="B", gross_margin=0.40, revenue_growth=0.10,
                               insider_net_buys_90d=3)
    flat = _bars([100 for _ in range(300)])
    assert quality_growth_score(with_buys, flat) > quality_growth_score(base, flat)


def test_none_profile_and_no_bars_is_none():
    assert quality_growth_score(None, None) is None
    assert quality_growth_score(None, []) is None
    # a profile with no usable fields and no bars -> None (fail-open, never penalized)
    assert quality_growth_score(CompanyProfile(symbol="X"), None) is None


def test_renormalizes_over_available_subscores():
    # only profitability present -> score is exactly the profitability sub-score
    prof_only = CompanyProfile(symbol="P", gross_profitability=0.50)
    b = quality_breakdown(prof_only, None)
    assert b["cash"] is None and b["growth"] is None and b["momentum"] is None
    assert b["profitability"] is not None
    assert b["score"] == pytest.approx(b["profitability"])


def test_momentum_only_from_bars():
    # no profile fields, but bars present -> momentum drives the score
    up = _bars([100 + i for i in range(300)])
    down = _bars([300 - i for i in range(300)])
    empty = CompanyProfile(symbol="M")
    s_up = quality_growth_score(empty, up)
    s_down = quality_growth_score(empty, down)
    assert s_up is not None and s_down is not None
    assert s_up > s_down


def test_momentum_only_capped_at_neutral():
    # no fundamentals at all + a hot chart must not masquerade as high quality (cap at 50);
    # momentum can still pull a no-fundamental name DOWN below 50.
    hot = _bars([100 + i for i in range(300)])
    cold = _bars([300 - i for i in range(300)])
    empty = CompanyProfile(symbol="TE")
    assert quality_growth_score(empty, hot) <= 50.0
    assert quality_growth_score(empty, cold) < 50.0
    # once ANY fundamental sub-score exists, the cap lifts (a real profitable grower can exceed 50)
    good = CompanyProfile(symbol="G", gross_margin=0.70, revenue_growth=0.25)
    assert quality_growth_score(good, hot) > 50.0


def test_below_sma200_haircuts_momentum():
    up = _bars([100 + i for i in range(300)])
    empty = CompanyProfile(symbol="M")
    above = quality_breakdown(empty, up, below_sma200=False)["momentum"]
    below = quality_breakdown(empty, up, below_sma200=True)["momentum"]
    assert below == pytest.approx(round(above * 0.8, 1))


def test_growth_sweet_spot_curve():
    def g(x):
        return quality_breakdown(CompanyProfile(symbol="G", revenue_growth=x), None)["growth"]
    # shrinking < modest < durable sweet spot; hypergrowth is haircut below the 30% peak
    assert g(-0.20) < g(0.05) < g(0.20)
    assert g(0.30) >= g(0.50)  # 30% is the peak; 50% is haircut (sustainability risk)


def test_momentum_fallback_short_history():
    # fewer than 253 bars uses the full-window fallback, still ordering up vs down
    up = quality_breakdown(CompanyProfile(symbol="S"), _bars([100 + i for i in range(50)]))["momentum"]
    down = quality_breakdown(CompanyProfile(symbol="S"), _bars([150 - i for i in range(50)]))["momentum"]
    assert up is not None and down is not None and up > down
