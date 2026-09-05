"""Entry-feature analytics: bucketing, win-rate/P&L math, resolved-only + empty safety."""
from agentic.services.analytics import build_feature_analytics


def _row(pnl, **kw):
    base = dict(underlying="ONDS", kind="csp", delta=-0.25, dte=10, iv=0.9,
                realized_pnl=pnl, ai_recommendation="proceed", ai_regime_label="calm",
                exit_reason="profit-trail", context={"rsi": 55, "above_sma200": True})
    base.update(kw)
    return base


def test_summary_counts_resolved_only():
    rows = [_row(10), _row(-5), _row(20), {"realized_pnl": None}]  # last is unresolved -> excluded
    s = build_feature_analytics(rows)["summary"]
    assert s["resolved_trades"] == 3
    assert s["wins"] == 2 and s["losses"] == 1
    assert s["win_rate"] == round(2 / 3, 3)
    assert s["total_pnl"] == 25.0
    assert s["avg_pnl"] == round(25 / 3, 2)


def test_buckets_by_underlying_and_delta():
    rows = [_row(10, underlying="ONDS", delta=-0.22),
            _row(-4, underlying="SMR", delta=-0.28),
            _row(6, underlying="ONDS", delta=-0.12)]
    a = build_feature_analytics(rows)
    by_u = {b["bucket"]: b for b in a["by_feature"]["underlying"]}
    assert by_u["ONDS"]["n"] == 2 and by_u["ONDS"]["wins"] == 2 and by_u["ONDS"]["win_rate"] == 1.0
    assert by_u["SMR"]["n"] == 1 and by_u["SMR"]["win_rate"] == 0.0 and by_u["SMR"]["avg_pnl"] == -4.0
    by_d = {b["bucket"]: b for b in a["by_feature"]["delta"]}
    assert set(by_d) == {"0.20-0.25", "0.25-0.30", "<0.15"}   # 0.22, 0.28, 0.12


def test_context_features_bucketed():
    rows = [_row(10, context={"rsi": 25, "above_sma200": False}),
            _row(8, context={"rsi": 60, "above_sma200": True})]
    a = build_feature_analytics(rows)
    by_rsi = {b["bucket"] for b in a["by_feature"]["rsi"]}
    assert by_rsi == {"<30", "55-70"}
    by_sma = {b["bucket"] for b in a["by_feature"]["above_sma200"]}
    assert by_sma == {"below", "above"}


def test_dte_and_iv_banding():
    rows = [_row(1, dte=5, iv=0.4), _row(1, dte=10, iv=0.9), _row(1, dte=40, iv=1.3)]
    a = build_feature_analytics(rows)
    assert {b["bucket"] for b in a["by_feature"]["dte"]} == {"0-7", "8-14", "31+"}
    assert {b["bucket"] for b in a["by_feature"]["iv"]} == {"<0.50", "0.80-1.10", "1.10+"}


def test_iv_rank_and_regime_buckets():
    rows = [_row(10, context={"iv_rank": 82, "mkt_regime": "calm"}),
            _row(-5, context={"iv_rank": 15, "mkt_regime": "risk_off"}),
            _row(6, context={"iv_rank": 60, "mkt_regime": "calm"})]
    a = build_feature_analytics(rows)
    by_ivr = {b["bucket"] for b in a["by_feature"]["iv_rank"]}
    assert by_ivr == {"75+", "<25", "50-75"}
    by_reg = {b["bucket"]: b for b in a["by_feature"]["mkt_regime"]}
    assert by_reg["calm"]["n"] == 2 and by_reg["risk_off"]["n"] == 1
    assert by_reg["risk_off"]["win_rate"] == 0.0


def test_empty_is_safe():
    a = build_feature_analytics([])
    assert a["summary"]["resolved_trades"] == 0
    assert a["summary"]["win_rate"] is None and a["summary"]["avg_pnl"] is None
    assert a["by_feature"]["delta"] == []


def test_iv_rv_ratio_bucket():
    rows = [_row(10, context={"iv_rv_ratio": 1.6}),   # implied >> realized (rich premium)
            _row(-5, context={"iv_rv_ratio": 0.8})]    # implied < realized (underpaid)
    a = build_feature_analytics(rows)
    buckets = {b["bucket"] for b in a["by_feature"]["iv_rv_ratio"]}
    assert buckets == {"1.4+", "<0.9"}
