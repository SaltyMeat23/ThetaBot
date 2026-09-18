"""Strike survival: touch / ITM rates by cushion and horizon, theory fields, label conditioning."""
from statistics import NormalDist

from agentic.config import SetupConfig
from agentic.services.strike_survival import labels_by_index, strike_survival


def _bars(closes):
    return [{"o": c, "h": c + 0.5, "l": c - 0.5, "c": c, "v": 1000} for c in closes]


def _row(rows, **want):
    for r in rows:
        if all(r.get(k) == v for k, v in want.items()):
            return r
    raise AssertionError(f"no row matching {want}")


def test_flat_series_never_touches_a_pct_cushion():
    rows = strike_survival({"X": _bars([100.0] * 120)}, unit="pct", cushions=(0.05,), horizons=(5, 10))
    r = _row(rows, symbol="ALL", label="ALL", cushion=0.05, horizon=10)
    assert r["n"] > 0 and r["touch_rate"] == 0.0 and r["itm_rate"] == 0.0
    assert r["avg_worst_pct"] == round((99.5 - 100) / 100, 4)             # the -0.5 low each bar
    assert r["p_itm_theory"] is None                                        # theory only for the em unit


def test_decline_touches_and_finishes_itm():
    closes = [100 - i * 0.4 for i in range(120)]                            # steady bleed, -40% total
    rows = strike_survival({"X": _bars(closes)}, unit="pct", cushions=(0.02,), horizons=(10,))
    r = _row(rows, symbol="ALL", label="ALL", cushion=0.02, horizon=10)
    assert r["touch_rate"] > 0.95 and r["itm_rate"] > 0.95                  # a 2% cushion never survives


def test_em_unit_theory_fields_and_pooling():
    closes = [100 + (i % 7) * 0.8 - 2 for i in range(140)]                  # some realized vol
    rows = strike_survival({"X": _bars(closes), "Y": _bars(closes)}, unit="em",
                           cushions=(1.0,), horizons=(5,))
    nd = NormalDist()
    r = _row(rows, symbol="ALL", label="ALL", cushion=1.0, horizon=5)
    assert r["p_itm_theory"] == round(nd.cdf(-1.0), 4) and r["p_touch_theory"] == round(2 * nd.cdf(-1.0), 4)
    x = _row(rows, symbol="X", label="ALL", cushion=1.0, horizon=5)
    assert r["n"] == 2 * x["n"]                                             # pooled = sum of names
    assert 0.0 <= r["touch_rate"] <= 1.0 and r["itm_rate"] <= r["touch_rate"]   # ITM implies touched


def test_label_conditioning_counts_only_labelled_bars():
    closes = [100.0] * 120
    rows = strike_survival({"X": _bars(closes)}, unit="pct", cushions=(0.05,), horizons=(5,),
                           labels_by_symbol={"X": {70: {"quiet_base"}, 71: {"quiet_base"}}})
    q = _row(rows, symbol="ALL", label="quiet_base", cushion=0.05, horizon=5)
    a = _row(rows, symbol="ALL", label="ALL", cushion=0.05, horizon=5)
    assert q["n"] == 2 and a["n"] > q["n"]


def test_labels_by_index_walk_forward():
    closes = [10.0 if i % 2 == 0 else 11.0 for i in range(70)] + [12.0, 12.05]
    lab = labels_by_index(_bars(closes), SetupConfig())
    assert "breakout" in lab[70]                                            # the break bar
    assert all(k >= 64 for k in lab)                                        # nothing before warm-up
