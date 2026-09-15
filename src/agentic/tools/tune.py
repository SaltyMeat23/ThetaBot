"""Outcome-weighted tuning: read the analytics flywheel, propose tightening-only entry-criteria changes.

Closes the loop the bot already half-has: analytics.py buckets resolved trades by feature (delta,
iv_rank, adx, bb_percent_b, quality_score) with win-rate/avg-P&L per bucket, but nothing acts on it.
This proposes moving an EntryCriteria threshold to EXCLUDE a losing bucket — surfaced as a suggestion
report + a shadow audit trail. It reuses tv_autopush.py's shape (default-deny allowlist, never-widen
direction check, sanity clamps, shadow-first, audit JSONL).

Two honest constraints baked in:
- Tightening-ONLY. We have outcomes only for trades we took, so we can exclude regions that lost but
  cannot confidently loosen into untraded regions.
- Small-n-gated. Every bucket needs n >= policy.min_n or it produces nothing. With few trades the
  correct output is "not enough data" — that is the point, not a bug.

Phase 1 is read-only (no POST). The live POST /api/config path is intentionally NOT wired here.

Run:  python -m agentic.tools.tune --api-base https://host   (prints report, writes data/tuning_audit.jsonl)
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

# --- the knob map: analytics dimension -> EntryCriteria field, split edge, tightening direction -----
# direction "raise" => min_* gate: the PERMISSIVE (excludable) side is value < edge; tighten by raising
#   the floor to `edge`. direction "lower" => *_max cap: permissive side is value >= edge; tighten by
#   lowering the cap to `edge`. `clamp` bounds the field; `max_step` caps a single move from a set value.
@dataclass(frozen=True)
class KnobSpec:
    dimension: str          # key on a normalized trade record
    field: str              # EntryCriteria field to tune
    edge: float             # candidate threshold (the proposed value)
    direction: Literal["raise", "lower"]
    clamp: tuple[float, float]
    max_step: float         # max change from a non-None current value in one proposal


KNOBS: tuple[KnobSpec, ...] = (
    KnobSpec("delta", "delta_max", 0.25, "lower", (0.10, 0.35), 0.05),
    KnobSpec("iv_rank", "min_iv_rank", 50.0, "raise", (0.0, 100.0), 15.0),
    KnobSpec("adx", "min_adx", 20.0, "raise", (0.0, 60.0), 5.0),
    KnobSpec("bb_percent_b", "min_bb_percent_b", 20.0, "raise", (0.0, 100.0), 15.0),
    KnobSpec("quality_score", "min_quality_score", 40.0, "raise", (0.0, 100.0), 10.0),
    KnobSpec("distance_to_support", "support_buffer_pct", 0.02, "raise", (0.0, 0.10), 0.02),  # Phase 2
)
ALLOWED_FIELDS = frozenset(k.field for k in KNOBS)


@dataclass(frozen=True)
class TightenPolicy:
    mode: Literal["shadow", "live"] = "shadow"
    min_n: int = 25              # per bucket; below this, no proposal
    win_rate_floor: float = 0.50  # a "losing" bucket is below this AND avg_pnl < 0
    scope: Literal["global", "per_ticker"] = "global"


@dataclass(frozen=True)
class TuneProposal:
    scope: str                  # "global" or a symbol
    field: str
    current_value: float | None
    proposed_value: float
    dimension: str
    reason: str
    evidence: dict[str, Any]    # {excluded: {n,win_rate,avg_pnl}, kept: {...}}


@dataclass(frozen=True)
class GuardResult:
    proposal: TuneProposal
    allowed: bool
    shadow: bool
    rejections: list[str] = field(default_factory=list)

    @property
    def would_apply(self) -> bool:
        return self.allowed and not self.shadow


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _bucket_stats(records: list[dict], dim: str, edge: float, side: Literal["below", "atleast"]) -> dict:
    """Aggregate win-rate / avg-pnl for records whose `dim` value is below / at-or-above `edge`."""
    n = wins = 0
    pnl = 0.0
    for r in records:
        v, p = r.get(dim), r.get("realized_pnl")
        if not _is_num(v) or not _is_num(p):
            continue
        in_side = (v < edge) if side == "below" else (v >= edge)
        if not in_side:
            continue
        n += 1
        pnl += float(p)
        if float(p) > 0:
            wins += 1
    return {"n": n, "win_rate": round(wins / n, 3) if n else None, "avg_pnl": round(pnl / n, 2) if n else None}


def propose_from_records(
    records: list[dict], current_criteria: dict, *, policy: TightenPolicy,
) -> list[TuneProposal]:
    """For each knob, if the permissive-end bucket lost (n>=min_n, win_rate<floor, avg_pnl<0) and the
    kept region is clearly better, propose moving the threshold to exclude it. Tightening only."""
    out: list[TuneProposal] = []
    for k in KNOBS:
        excl_side = "atleast" if k.direction == "lower" else "below"  # the side we'd exclude
        keep_side = "below" if k.direction == "lower" else "atleast"
        excl = _bucket_stats(records, k.dimension, k.edge, excl_side)
        keep = _bucket_stats(records, k.dimension, k.edge, keep_side)
        if excl["n"] < policy.min_n or excl["win_rate"] is None:
            continue
        losing = excl["win_rate"] < policy.win_rate_floor and (excl["avg_pnl"] or 0) < 0
        better = keep["n"] > 0 and keep["win_rate"] is not None and keep["win_rate"] > excl["win_rate"]
        if not (losing and better):
            continue
        cur = current_criteria.get(k.field)
        cur = float(cur) if _is_num(cur) else None
        # Skip proposals the guard is structurally guaranteed to reject: if the current threshold is
        # already at/beyond the edge, moving to the edge is not a tightening move. Avoids report noise.
        if cur is not None and ((k.direction == "raise" and k.edge <= cur)
                                or (k.direction == "lower" and k.edge >= cur)):
            continue
        out.append(TuneProposal(
            scope="global", field=k.field, current_value=cur, proposed_value=k.edge,
            dimension=k.dimension,
            reason=(f"{k.dimension} {'>=' if k.direction=='lower' else '<'}{k.edge} bucket: "
                    f"{excl['win_rate']*100:.0f}% win, {excl['avg_pnl']} avg P&L over n={excl['n']} "
                    f"vs kept {keep['win_rate']*100:.0f}%/{keep['avg_pnl']}"),
            evidence={"excluded": excl, "kept": keep},
        ))
    return out


def evaluate_tune(proposal: TuneProposal, *, policy: TightenPolicy) -> GuardResult:
    """Run every guard. `allowed` reflects a live apply; shadow suppresses the write."""
    rej: list[str] = []
    shadow = policy.mode != "live"
    spec = next((k for k in KNOBS if k.field == proposal.field), None)

    if proposal.field not in ALLOWED_FIELDS or spec is None:
        rej.append(f"{proposal.field!r} not in tightening-only allowlist")
        return GuardResult(proposal, allowed=False, shadow=shadow, rejections=rej)

    v = proposal.proposed_value
    if not _is_num(v):
        rej.append("proposed value not finite")
    else:
        lo, hi = spec.clamp
        if not (lo <= v <= hi):
            rej.append(f"proposed {v} outside clamp [{lo}, {hi}]")

    cur = proposal.current_value
    # never-widen: raising a floor / lowering a cap tightens; the opposite widens risk.
    if _is_num(cur):
        if spec.direction == "raise" and v <= cur:
            rej.append("not a tightening move (would lower/keep the floor -> widens risk)")
        if spec.direction == "lower" and v >= cur:
            rej.append("not a tightening move (would raise/keep the cap -> widens risk)")
        if abs(v - float(cur)) > spec.max_step + 1e-9:
            rej.append(f"step {abs(v-float(cur)):.4f} exceeds max_step {spec.max_step}")
    # cur is None -> enabling a gate from off; that is tightening, allowed (no step limit).

    n = (proposal.evidence.get("excluded") or {}).get("n", 0)
    if not isinstance(n, int) or n < policy.min_n:
        rej.append(f"evidence n={n} below min_n {policy.min_n}")

    return GuardResult(proposal, allowed=not rej, shadow=shadow, rejections=rej)


# --- audit + report -----------------------------------------------------------------------------

def audit_entry(result: GuardResult) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": "shadow" if result.shadow else "live",
        "scope": result.proposal.scope,
        "field": result.proposal.field,
        "current": result.proposal.current_value,
        "proposed": result.proposal.proposed_value,
        "dimension": result.proposal.dimension,
        "reason": result.proposal.reason,
        "allowed": result.allowed,
        "applied": result.would_apply,
        "rejections": result.rejections,
    }


def append_audit(entry: dict, path) -> None:
    from pathlib import Path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def build_report(results: list[GuardResult], *, resolved_trades: int, policy: TightenPolicy) -> str:
    lines = [
        "# Outcome-weighted tuning suggestions",
        "",
        f"_Resolved trades analyzed: **{resolved_trades}** | mode: **{policy.mode}** | "
        f"min_n per bucket: **{policy.min_n}** | tightening-only._",
        "",
        "> NOTE: Descriptive, not predictive. Includes paper trades (no live-only filter yet). "
        "Buckets are noisy below ~200 trades - treat as a prompt to review, not a mandate.",
        "",
    ]
    allowed = [r for r in results if r.allowed]
    if not allowed:
        lines.append("**No changes proposed** - no bucket met the evidence bar "
                     f"(n>={policy.min_n}, losing, with a clearly better kept region). "
                     "This is the expected result until more trades accumulate.")
    for r in allowed:
        p = r.proposal
        cur = "off/None" if p.current_value is None else p.current_value
        lines.append(f"### {p.field}: `{cur}` -> `{p.proposed_value}`  ({p.scope})")
        lines.append(f"- {p.reason}")
        lines.append(f"- {'WOULD APPLY' if r.would_apply else 'shadow - no change written'}")
    blocked = [r for r in results if not r.allowed]
    if blocked:
        lines += ["", "_Rejected by guardrails:_"]
        for r in blocked:
            lines.append(f"- {r.proposal.field} -> {r.proposal.proposed_value}: {'; '.join(r.rejections)}")
    return "\n".join(lines)


# --- driver (I/O; not imported by tests). Phase 1: read-only, no POST. ---------------------------

def normalize_rows(rows: list[dict]) -> list[dict]:
    """Refinement-export rows -> normalized records the engine buckets on (resolved trades only)."""
    recs: list[dict] = []
    for row in rows:
        pnl = row.get("realized_pnl")
        if pnl is None:
            continue
        ctx = row.get("context")
        if isinstance(ctx, str):
            try:
                ctx = json.loads(ctx)
            except Exception:  # noqa: BLE001
                ctx = {}
        ctx = ctx or {}
        d = row.get("delta")
        recs.append({
            "realized_pnl": pnl,
            "delta": abs(d) if _is_num(d) else None,
            "iv_rank": ctx.get("iv_rank"),
            "adx": ctx.get("adx"),
            "bb_percent_b": ctx.get("bb_percent_b"),
            "quality_score": ctx.get("quality_score"),
            "distance_to_support": ctx.get("distance_to_support"),
        })
    return recs


def _audit_path():
    from ..config import REPO_ROOT
    return REPO_ROOT / "data" / "tuning_audit.jsonl"


def _main(argv: list[str]) -> int:
    import os
    from .tv_reconcile import _get_json
    p = argparse.ArgumentParser(description="Outcome-weighted entry-criteria tuning suggestions (read-only).")
    p.add_argument("--api-base", required=True)
    p.add_argument("--min-n", type=int, default=25)
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)

    user, password = os.environ.get("DASHBOARD_USER"), os.environ.get("DASHBOARD_PASSWORD")
    base = args.api_base.rstrip("/")
    export = _get_json(f"{base}/api/refinement-export?limit={args.limit}&format=json", user, password)
    rows = export.get("rows", [])
    cfg = _get_json(f"{base}/api/config", user, password)
    criteria = ((cfg.get("editable") or {}).get("entry") or {}).get("criteria") or {}

    records = normalize_rows(rows)
    policy = TightenPolicy(mode="shadow", min_n=args.min_n)  # Phase 1: shadow only, never POSTs
    proposals = propose_from_records(records, criteria, policy=policy)
    results = [evaluate_tune(pr, policy=policy) for pr in proposals]
    for r in results:
        append_audit(audit_entry(r), _audit_path())

    if args.json:
        print(json.dumps({"resolved_trades": len(records),
                          "proposals": [asdict(r.proposal) | {"allowed": r.allowed,
                                        "rejections": r.rejections} for r in results]}, indent=2))
    else:
        print(build_report(results, resolved_trades=len(records), policy=policy))
    return 0


def main() -> None:
    raise SystemExit(_main(sys.argv[1:]))


if __name__ == "__main__":
    main()
