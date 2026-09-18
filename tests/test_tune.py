"""Outcome-weighted tuning engine: proposal generation + tightening-only guardrails. Pure, offline."""
import json

from agentic.tools.tune import (
    TightenPolicy,
    TuneProposal,
    append_audit,
    audit_entry,
    build_report,
    evaluate_tune,
    normalize_rows,
    propose_from_records,
)

POLICY = TightenPolicy(min_n=4)  # small n so fixtures stay compact


def _recs(dim, losing_val, winning_val, n_each=5):
    """n_each losing records at losing_val + n_each winning at winning_val for dimension `dim`."""
    return ([{dim: losing_val, "realized_pnl": -10.0} for _ in range(n_each)]
            + [{dim: winning_val, "realized_pnl": 12.0} for _ in range(n_each)])


def _prop(field, current, proposed, dim="delta", n=10):
    return TuneProposal(scope="global", field=field, current_value=current, proposed_value=proposed,
                        dimension=dim, reason="x", evidence={"excluded": {"n": n}, "kept": {"n": n}})


# --- proposal generation ------------------------------------------------------------------------

def test_proposes_lowering_delta_max_from_losing_high_bucket():
    recs = _recs("delta", 0.28, 0.20)  # >=0.25 loses, <0.25 wins
    props = propose_from_records(recs, {"delta_max": 0.30}, policy=POLICY)
    p = next(p for p in props if p.field == "delta_max")
    assert p.current_value == 0.30 and p.proposed_value == 0.25
    assert evaluate_tune(p, policy=POLICY).allowed


def test_proposes_enabling_min_adx_from_losing_low_bucket():
    recs = _recs("adx", 15.0, 30.0)  # adx<20 loses, >=20 wins
    props = propose_from_records(recs, {}, policy=POLICY)  # min_adx currently off
    p = next(p for p in props if p.field == "min_adx")
    assert p.current_value is None and p.proposed_value == 20.0
    assert evaluate_tune(p, policy=POLICY).allowed  # enabling a gate is tightening


def test_small_n_produces_no_proposal():
    recs = _recs("delta", 0.28, 0.20, n_each=2)  # only 2 in each bucket, below min_n=4
    assert propose_from_records(recs, {"delta_max": 0.30}, policy=POLICY) == []


def test_no_proposal_when_already_tighter_than_edge():
    # losing >=0.25 bucket, but delta_max is already 0.20 (tighter than the 0.25 edge) -> a same-edge
    # proposal would only be rejected by the guard, so it must not be generated at all.
    recs = _recs("delta", 0.28, 0.20)
    assert propose_from_records(recs, {"delta_max": 0.20}, policy=POLICY) == []


def test_no_proposal_when_permissive_bucket_wins():
    # high-delta bucket actually wins -> nothing to tighten
    recs = ([{"delta": 0.28, "realized_pnl": 12.0} for _ in range(5)]
            + [{"delta": 0.20, "realized_pnl": -10.0} for _ in range(5)])
    assert propose_from_records(recs, {"delta_max": 0.30}, policy=POLICY) == []


# --- guardrails ---------------------------------------------------------------------------------

def test_allowlist_rejects_non_knob_field():
    r = evaluate_tune(_prop("dte_max", 45, 30), policy=POLICY)
    assert not r.allowed and any("allowlist" in x for x in r.rejections)


def test_never_widen_raising_a_cap_rejected():
    r = evaluate_tune(_prop("delta_max", 0.25, 0.30), policy=POLICY)  # raising the cap widens risk
    assert not r.allowed and any("widens risk" in x for x in r.rejections)


def test_never_widen_lowering_a_floor_rejected():
    r = evaluate_tune(_prop("min_adx", 25.0, 20.0, dim="adx"), policy=POLICY)  # lowering floor widens
    assert not r.allowed and any("widens risk" in x for x in r.rejections)


def test_tightening_moves_allowed():
    assert evaluate_tune(_prop("delta_max", 0.30, 0.25), policy=POLICY).allowed      # lower cap
    assert evaluate_tune(_prop("min_adx", 15.0, 20.0, dim="adx"), policy=POLICY).allowed  # raise floor


def test_max_step_enforced():
    r = evaluate_tune(_prop("delta_max", 0.35, 0.25), policy=POLICY)  # 0.10 move > max_step 0.05
    assert not r.allowed and any("max_step" in x for x in r.rejections)


def test_clamp_enforced():
    r = evaluate_tune(_prop("delta_max", 0.12, 0.05), policy=POLICY)  # below clamp floor 0.10
    assert not r.allowed and any("clamp" in x for x in r.rejections)


def test_below_min_n_rejected():
    r = evaluate_tune(_prop("delta_max", 0.30, 0.25, n=2), policy=POLICY)
    assert not r.allowed and any("min_n" in x for x in r.rejections)


def test_shadow_never_applies_and_audit_written(tmp_path):
    r = evaluate_tune(_prop("delta_max", 0.30, 0.25), policy=POLICY)  # default shadow
    assert r.allowed and r.shadow and r.would_apply is False
    path = tmp_path / "tuning_audit.jsonl"
    append_audit(audit_entry(r), path)
    rec = json.loads(path.read_text().strip())
    assert rec["mode"] == "shadow" and rec["applied"] is False and rec["field"] == "delta_max"


# --- normalization + report ---------------------------------------------------------------------

def test_normalize_rows_extracts_and_filters():
    rows = [
        {"delta": -0.28, "realized_pnl": -10.0, "context": {"adx": 18.0, "iv_rank": 40.0}},
        {"delta": -0.20, "realized_pnl": 12.0, "context": json.dumps({"adx": 30.0})},  # str context
        {"delta": -0.25, "realized_pnl": None, "context": {}},  # unresolved -> dropped
    ]
    recs = normalize_rows(rows)
    assert len(recs) == 2                         # unresolved filtered out
    assert recs[0]["delta"] == 0.28               # abs()
    assert recs[0]["adx"] == 18.0 and recs[0]["iv_rank"] == 40.0
    assert recs[1]["adx"] == 30.0                 # parsed from JSON-string context


def test_report_says_nothing_when_no_proposals():
    md = build_report([], resolved_trades=14, policy=POLICY)
    assert "No changes proposed" in md and "14" in md and "paper trades" in md


# --- categorical knob: avoid_setups (technical setups; tightening-only) --------------------------

from agentic.tools.tune import evaluate_setup_avoid, propose_setup_avoid  # noqa: E402


def _setup_recs(label, n_lose, n_win_other):
    recs = [{"realized_pnl": -10.0, "primary_setup": label} for _ in range(n_lose)]
    recs += [{"realized_pnl": 12.0, "primary_setup": "washout"} for _ in range(n_win_other)]
    return recs


def test_propose_setup_avoid_fires_only_on_losing_bucket_with_evidence():
    pol = TightenPolicy(mode="shadow", min_n=25)
    props = propose_setup_avoid(_setup_recs("breakdown", 30, 20), {"avoid_setups": None}, policy=pol)
    assert len(props) == 1 and props[0].field == "avoid_setups"
    assert props[0].proposed_value == ["breakdown"] and props[0].evidence["added"] == "breakdown"
    assert propose_setup_avoid(_setup_recs("breakdown", 10, 20), {"avoid_setups": None}, policy=pol) == []
    assert propose_setup_avoid(_setup_recs("breakdown", 30, 20), {"avoid_setups": ["breakdown"]}, policy=pol) == []
    winning = [{"realized_pnl": 5.0, "primary_setup": "coiling"} for _ in range(30)] + _setup_recs("breakdown", 0, 5)
    assert propose_setup_avoid(winning, {}, policy=pol) == []       # a winning bucket is never proposed


def test_evaluate_setup_avoid_guards():
    pol = TightenPolicy(mode="shadow", min_n=25)
    ok = TuneProposal("global", "avoid_setups", None, ["breakdown", "support_break"], "primary_setup", "x",
                      {"excluded": {"n": 30}, "current": ["support_break"], "added": "breakdown"})
    r = evaluate_setup_avoid(ok, policy=pol)
    assert r.allowed and r.shadow and not r.would_apply             # shadow: never writes
    rm = TuneProposal("global", "avoid_setups", None, ["breakdown"], "primary_setup", "x",
                      {"excluded": {"n": 30}, "current": ["support_break"], "added": "breakdown"})
    assert any("removes" in m for m in evaluate_setup_avoid(rm, policy=pol).rejections)
    two = TuneProposal("global", "avoid_setups", None, ["breakdown", "falling_knife"], "primary_setup", "x",
                       {"excluded": {"n": 30}, "current": [], "added": "breakdown"})
    assert any("exactly one" in m for m in evaluate_setup_avoid(two, policy=pol).rejections)
    bad = TuneProposal("global", "avoid_setups", None, ["nonsense"], "primary_setup", "x",
                       {"excluded": {"n": 30}, "current": []})
    assert any("known setup labels" in m for m in evaluate_setup_avoid(bad, policy=pol).rejections)
    low = TuneProposal("global", "avoid_setups", None, ["breakdown"], "primary_setup", "x",
                       {"excluded": {"n": 5}, "current": []})
    assert any("min_n" in m for m in evaluate_setup_avoid(low, policy=pol).rejections)
    num = TuneProposal("global", "min_adx", None, 20.0, "adx", "x", {"excluded": {"n": 30}})
    assert not evaluate_setup_avoid(num, policy=pol).allowed          # numeric knobs use evaluate_tune
