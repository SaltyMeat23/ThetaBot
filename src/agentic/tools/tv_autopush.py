"""Auto-push verified structural values to the bot — guardrailed, shadow-first.

After reconciliation (tv_reconcile) shows the bot's feed diverged, this can push a corrected
structural ``support``/``resistance`` to POST /webhook/tradingview, or a tightening-only per-ticker
config tweak to POST /api/config. It NEVER touches mode/live/broker/risk-widening fields, sanity-ranges
every value, requires a reconciliation justification (never blind), tags webhook pushes with
``source="mcp_reconcile"``, and defaults to SHADOW (log what it WOULD push, POST nothing). Every
evaluation is appended to data/tv_autopush_audit.jsonl — the evidence for graduating to live.

Note web/settings.py's own allowlist permits the whole ``entry`` subtree (incl. risk-wideners); this
layer is the real safety boundary — its config allowlist is far narrower and tightening-only.

Run:  python -m agentic.tools.tv_autopush --api-base https://host --symbols F --chart-json chart.json \
          [--mode shadow|live]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from .tv_reconcile import ReconcileTolerance, SymbolReport, reconcile

# --- allowlists (default-deny) ------------------------------------------------------------------
WEBHOOK_ALLOWED = frozenset({"support", "resistance"})
# Config: tightening-only knobs that ACTUALLY EXIST in EntryCriteria today. NEVER delta_*, dte_*,
# min_annualized_yield, sizing.*, max_pct_below_sma200, or anything under execution/ai/risk. The
# resistance_* variants are Phase-5 candidates (no such fields yet) — add them here only once
# EntryCriteria defines them, else a live push would target a nonexistent field.
CONFIG_BUFFER_FIELDS = frozenset({"support_buffer_pct"})
CONFIG_ENABLE_FIELDS = frozenset({"require_strike_below_support"})
CONFIG_ALLOWED = CONFIG_BUFFER_FIELDS | CONFIG_ENABLE_FIELDS

# Divergence kinds a value-push legitimately corrects.
_CORRECTABLE = {"numeric_drift", "lingering_on_bot", "missing_on_bot", "unconsumed_alias", "type_mismatch"}


@dataclass(frozen=True)
class PushPolicy:
    mode: Literal["shadow", "live"] = "shadow"
    max_level_pct: float = 0.15   # a pushed S/R level must be within 15% of live price
    buffer_cap: float = 0.10      # buffers clamped to [0, 10%]


@dataclass(frozen=True)
class PushProposal:
    target: Literal["webhook", "config"]
    symbol: str
    field: str
    value: Any
    reason: str


@dataclass(frozen=True)
class GuardResult:
    proposal: PushProposal
    allowed: bool
    shadow: bool
    rejections: list[str] = field(default_factory=list)

    @property
    def would_post(self) -> bool:
        return self.allowed and not self.shadow


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _recon_justifies(proposal: PushProposal, recon: SymbolReport | None) -> str | None:
    """Return a rejection string if reconciliation does not justify the push, else None.

    Never blind: a webhook value-push requires a CORRECTABLE divergence on that exact field (pushing a
    value the bot already has right is pointless and outside the "corrects a divergence" mandate); a
    config tweak requires the symbol's reconciliation to be non-clean (some divergence to act on)."""
    if recon is None or recon.symbol.upper() != proposal.symbol.upper():
        return "no reconciliation for this symbol (would be a blind push)"
    if proposal.target == "webhook":
        if not any(d.field == proposal.field and d.kind in _CORRECTABLE for d in recon.divergences):
            return f"no correctable {proposal.field} divergence to justify the push"
    elif recon.ok:  # config target
        return "reconciliation is clean — no divergence to justify a config change"
    return None


def evaluate_push(
    proposal: PushProposal, *, live_price: float | None,
    current_bot_value: Any, recon: SymbolReport | None, policy: PushPolicy,
) -> GuardResult:
    """Run every guard. ``allowed`` reflects what would happen live; shadow suppresses the POST."""
    rej: list[str] = []
    shadow = policy.mode != "live"

    # 1. Field allowlist per target.
    allowed_fields = WEBHOOK_ALLOWED if proposal.target == "webhook" else CONFIG_ALLOWED
    if proposal.field not in allowed_fields:
        rej.append(f"{proposal.field!r} not in {proposal.target} allowlist")

    if proposal.target == "webhook":
        # 3. Sanity ranges for a structural level.
        if not _finite(proposal.value):
            rej.append("value not finite")
        elif live_price is None or not _finite(live_price):
            rej.append("no live price to sanity-range against")
        else:
            v, p = float(proposal.value), float(live_price)
            if v <= 0:
                rej.append("level must be positive")
            if abs(v - p) / p > policy.max_level_pct:
                rej.append(f"level {v} is >{policy.max_level_pct*100:.0f}% from price {p}")
            if proposal.field == "support" and v >= p:
                rej.append("support must be below price")
            if proposal.field == "resistance" and v <= p:
                rej.append("resistance must be above price")
    else:  # config
        if proposal.field in CONFIG_BUFFER_FIELDS:
            if not _finite(proposal.value):
                rej.append("buffer not finite")
            else:
                v = float(proposal.value)
                if not (0.0 <= v <= policy.buffer_cap):
                    rej.append(f"buffer {v} outside [0, {policy.buffer_cap}]")
                # 2. Never-widen: raising a buffer tightens; lowering widens.
                if _finite(current_bot_value) and v < float(current_bot_value):
                    rej.append("lowering the buffer widens risk")
        elif proposal.field in CONFIG_ENABLE_FIELDS:
            # 2. Never-widen: only enabling a gate is allowed; disabling widens.
            if proposal.value is not True:
                rej.append("only enabling a gate is allowed (disabling widens risk)")

    # 4. Reconciliation precondition (never blind).
    why = _recon_justifies(proposal, recon)
    if why:
        rej.append(why)

    return GuardResult(proposal, allowed=not rej, shadow=shadow, rejections=rej)


def audit_entry(result: GuardResult, *, http_status: int | None = None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow" if result.shadow else "live",
        "target": result.proposal.target,
        "symbol": result.proposal.symbol,
        "field": result.proposal.field,
        "value": result.proposal.value,
        "reason": result.proposal.reason,
        "allowed": result.allowed,
        "posted": result.would_post and http_status is not None,
        "rejections": result.rejections,
        "http_status": http_status,
    }


def append_audit(entry: dict, path) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def webhook_body(proposal: PushProposal) -> dict:
    """The JSON to POST for a webhook push — tagged so operator corrections are distinguishable."""
    return {
        "action": "indicator", "symbol": proposal.symbol, proposal.field: proposal.value,
        "source": "mcp_reconcile", "pushed_at": datetime.now(timezone.utc).isoformat(),
    }


def proposals_from_reports(reports: list[SymbolReport], chart_by_symbol: dict[str, dict]) -> list[PushProposal]:
    """Derive webhook support/resistance corrections from reconciliation divergences."""
    out: list[PushProposal] = []
    for r in reports:
        chart = chart_by_symbol.get(r.symbol.upper()) or chart_by_symbol.get(r.symbol) or {}
        for d in r.divergences:
            if d.field in WEBHOOK_ALLOWED and d.kind in _CORRECTABLE and chart.get(d.field) is not None:
                out.append(PushProposal("webhook", r.symbol, d.field, chart[d.field],
                                        reason=f"{d.kind}: {d.detail}"))
    return out


# --- driver (I/O; not imported by tests) --------------------------------------------------------

def _audit_path():
    from ..config import REPO_ROOT
    return REPO_ROOT / "data" / "tv_autopush_audit.jsonl"


def _post_json(url: str, body: dict, headers: dict) -> int:
    import urllib.error
    import urllib.request
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310 (operator-run, trusted host)
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return -1  # connection refused / DNS / timeout — return a sentinel so the audit still records it


def _main(argv: list[str]) -> int:
    import os
    from .tv_reconcile import _get_json
    p = argparse.ArgumentParser(description="Guardrailed auto-push of verified structural values.")
    p.add_argument("--api-base", required=True)
    p.add_argument("--symbols", required=True)
    p.add_argument("--chart-json", required=True, help="chart values JSON ('-' for stdin)")
    p.add_argument("--mode", choices=["shadow", "live"], default="shadow")
    p.add_argument("--max-age", type=int)
    args = p.parse_args(argv)

    raw = sys.stdin.read() if args.chart_json == "-" else open(args.chart_json).read()
    chart_by_symbol = json.loads(raw)
    user, password = os.environ.get("DASHBOARD_USER"), os.environ.get("DASHBOARD_PASSWORD")
    wh_token = os.environ.get("TRADINGVIEW_WEBHOOK_TOKEN")
    base = args.api_base.rstrip("/")
    indicators = _get_json(f"{base}/api/tv-indicators", user, password).get("indicators", [])
    max_age = args.max_age or 108_000

    reports = reconcile(chart_by_symbol, indicators, tol=ReconcileTolerance(), max_age_seconds=max_age)
    by_sym = {r.symbol.upper(): r for r in reports}
    bot_by_sym = {str(r.get("symbol", "")).upper(): (r.get("payload") or {}) for r in indicators}
    policy = PushPolicy(mode=args.mode)
    proposals = proposals_from_reports(reports, chart_by_symbol)
    if not proposals:
        print("No correctable divergences — nothing to push.")
        return 0

    posted = 0
    for prop in proposals:
        chart = chart_by_symbol.get(prop.symbol.upper()) or {}
        res = evaluate_push(
            prop, live_price=chart.get("price"),
            current_bot_value=bot_by_sym.get(prop.symbol.upper(), {}).get(prop.field),
            recon=by_sym.get(prop.symbol.upper()), policy=policy)
        status = None
        if res.would_post and prop.target == "webhook":
            status = _post_json(f"{base}/webhook/tradingview", webhook_body(prop),
                                {"X-Webhook-Token": wh_token or ""})
            posted += 1
        append_audit(audit_entry(res, http_status=status), _audit_path())
        verb = "PUSHED" if status is not None else ("WOULD push" if res.allowed else "REJECTED")
        print(f"[{verb}] {prop.symbol} {prop.field}={prop.value}"
              + (f"  ({'; '.join(res.rejections)})" if res.rejections else ""))
    print(f"\nMode={args.mode} · {len(proposals)} proposal(s) · {posted} posted · "
          f"audit -> {_audit_path()}")
    return 0


def main() -> None:
    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
