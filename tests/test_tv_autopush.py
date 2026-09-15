"""Auto-push guardrails: allowlist, never-widen, sanity ranges, recon precondition, shadow. Pure."""
import json

from agentic.tools.tv_autopush import (
    PushPolicy,
    PushProposal,
    append_audit,
    audit_entry,
    evaluate_push,
    webhook_body,
)
from agentic.tools.tv_reconcile import ReconcileTolerance, reconcile_symbol

LIVE = PushPolicy(mode="live")


def _recon(symbol="F", chart=None, bot=None):
    chart = chart if chart is not None else {"support": 9.55}
    bot = bot if bot is not None else {"support": 9.20}  # drifted -> correctable
    return reconcile_symbol(symbol, chart, bot, 100.0, tol=ReconcileTolerance(stale_after_seconds=108_000))


def _support(value=9.55):
    return PushProposal("webhook", "F", "support", value, reason="numeric_drift")


def test_webhook_allowlist_rejects_adx():
    r = evaluate_push(PushProposal("webhook", "F", "adx", 22.0, "x"),
                      live_price=9.56, current_bot_value=None, recon=_recon(), policy=LIVE)
    assert not r.allowed and any("allowlist" in x for x in r.rejections)


def test_config_allowlist_rejects_delta_max():
    r = evaluate_push(PushProposal("config", "F", "delta_max", 0.22, "x"),
                      live_price=None, current_bot_value=0.30, recon=_recon(), policy=LIVE)
    assert not r.allowed and any("allowlist" in x for x in r.rejections)


def test_support_push_allowed():
    r = evaluate_push(_support(9.55), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=LIVE)
    assert r.allowed and r.would_post


def test_support_above_price_rejected():
    r = evaluate_push(_support(9.99), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=LIVE)
    assert not r.allowed and any("below price" in x for x in r.rejections)


def test_support_far_from_price_rejected():
    r = evaluate_push(_support(5.0), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=LIVE)
    assert not r.allowed and any("from price" in x for x in r.rejections)


def test_nan_value_rejected():
    r = evaluate_push(_support(float("nan")), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=LIVE)
    assert not r.allowed and any("finite" in x for x in r.rejections)


def test_lowering_buffer_rejected_raising_allowed():
    lo = evaluate_push(PushProposal("config", "F", "support_buffer_pct", 0.01, "tighten"),
                       live_price=None, current_bot_value=0.03, recon=_recon(), policy=LIVE)
    assert not lo.allowed and any("widens risk" in x for x in lo.rejections)
    hi = evaluate_push(PushProposal("config", "F", "support_buffer_pct", 0.05, "tighten"),
                       live_price=None, current_bot_value=0.03, recon=_recon(), policy=LIVE)
    assert hi.allowed


def test_disabling_gate_rejected():
    r = evaluate_push(PushProposal("config", "F", "require_strike_below_support", False, "x"),
                      live_price=None, current_bot_value=True, recon=_recon(), policy=LIVE)
    assert not r.allowed and any("disabling widens" in x for x in r.rejections)


def test_blind_push_rejected_without_recon():
    r = evaluate_push(_support(9.55), live_price=9.56, current_bot_value=9.20,
                      recon=None, policy=LIVE)
    assert not r.allowed and any("blind" in x for x in r.rejections)


def _clean_recon(symbol="F"):
    # chart and bot agree -> no divergences (SymbolReport.ok is True)
    return reconcile_symbol(symbol, {"support": 9.55}, {"support": 9.55}, 100.0,
                            tol=ReconcileTolerance(stale_after_seconds=108_000))


def test_webhook_push_rejected_when_field_has_no_divergence():
    # value the bot already has correct -> nothing to correct -> not justified (never blind).
    r = evaluate_push(_support(9.55), live_price=9.56, current_bot_value=9.55,
                      recon=_clean_recon(), policy=LIVE)
    assert not r.allowed and any("no correctable support divergence" in x for x in r.rejections)


def test_config_push_rejected_on_clean_recon():
    prop = PushProposal("config", "F", "support_buffer_pct", 0.05, "tighten")
    r = evaluate_push(prop, live_price=None, current_bot_value=0.03,
                      recon=_clean_recon(), policy=LIVE)
    assert not r.allowed and any("clean" in x for x in r.rejections)


def test_shadow_never_posts():
    r = evaluate_push(_support(9.55), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=PushPolicy())  # default shadow
    assert r.allowed is True and r.shadow is True and r.would_post is False


def test_webhook_body_carries_provenance():
    body = webhook_body(_support(9.55))
    assert body["source"] == "mcp_reconcile" and body["action"] == "indicator"
    assert body["support"] == 9.55 and "pushed_at" in body


def test_audit_written(tmp_path):
    r = evaluate_push(_support(9.55), live_price=9.56, current_bot_value=9.20,
                      recon=_recon(), policy=PushPolicy())
    path = tmp_path / "audit.jsonl"
    append_audit(audit_entry(r), path)
    lines = path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["mode"] == "shadow" and rec["field"] == "support" and rec["posted"] is False
