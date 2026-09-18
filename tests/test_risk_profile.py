"""Per-ticker risk profile: strike-survival summary, cushion suggestion, tightening-only proposals."""
from agentic.services.risk_profile import GRID, propose_ticker_cushions, ticker_risk_profile


def _bars(closes):
    return [{"o": c, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 1000} for c in closes]


def test_profile_flat_series_is_unreliable():
    p = ticker_risk_profile(_bars([100.0] * 150))       # zero realized vol -> no em cushions at all
    assert p["n"] == 0 and p["reliable"] is False and p["suggested_cushion"] is None


def test_profile_calm_name_ok_at_base_and_violent_name_needs_tightening():
    calm = [100 + (i % 5) * 0.2 for i in range(220)]                     # small, contained wiggles
    pc = ticker_risk_profile(_bars(calm), target_itm=0.20)
    assert pc["reliable"] and pc["n"] >= 60 and pc["by_cushion"]
    # a name that keeps gapping down: the base cushion can't hold the ITM rate under target
    violent = []
    x = 100.0
    for i in range(220):
        x = x * (0.94 if i % 3 == 0 else 1.02)
        violent.append(x)
    pv = ticker_risk_profile(_bars(violent), target_itm=0.05)
    assert pv["reliable"] and pv["needs_tightening"] and pv["suggested_cushion"] > 0.7
    assert pv["suggested_cushion"] in GRID


def test_propose_ticker_cushions_is_tightening_only():
    profiles = {
        "AAA": {"reliable": True, "needs_tightening": True, "suggested_cushion": 1.25, "itm_rate": 0.28,
                "horizon": 10, "target_itm": 0.20, "n": 200},
        "BBB": {"reliable": True, "needs_tightening": False, "suggested_cushion": 0.7, "itm_rate": 0.15,
                "horizon": 10, "target_itm": 0.20, "n": 200},
        "CCC": {"reliable": False, "needs_tightening": True, "suggested_cushion": 2.0},   # thin history -> skip
        "DDD": {"reliable": True, "needs_tightening": True, "suggested_cushion": 1.0, "itm_rate": 0.25,
                "horizon": 10, "target_itm": 0.20, "n": 200},
    }
    per_ticker = {"DDD": {"min_strike_expected_moves": 1.5}}          # already wider -> never loosened
    props = propose_ticker_cushions(profiles, per_ticker)
    assert [p["symbol"] for p in props] == ["AAA"]
    assert props[0]["proposed"] == 1.25 and props[0]["field"] == "min_strike_expected_moves"
    assert "28% of puts" in props[0]["reason"]
