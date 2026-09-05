"""Entry risk / position-sizing layer — the dedicated RMS run just before order release.

Kept separate from the screener (research: risk checks must be their own layer, not embedded in
strategy logic). Given screened candidates plus account state, it sizes each entry and enforces
the hard caps, returning only approved, sized entries.

Caps enforced (scale-invariant when target_positions is set — the same config sizes correctly from
$5k to $500k):
  - diversification (opt-in): aim to spread committed collateral across ~target_positions names, so
    the per-name budget/% shrinks automatically as the account grows
  - per-name: each CSP <= max_position_size_pct of account value (a hard BACKSTOP under
    target_positions sizing — only binds at small accounts)
  - portfolio: total committed CSP collateral <= total_bp_utilization_target of account value
  - buying-power reserve: never use more than (1 - reserve_pct) of buying power this run
  - liquidity (opt-in): never take more than max_pct_of_oi of a strike's open interest (fail-open on
    unknown OI) — stops a large account over-filling thin options
  - position count: min(target_positions, max_concurrent_positions) across open CSPs
  - one CSP per underlying (no doubling up); skip names already held
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import EntrySizing
from ..domain.enums import Direction, OptionType
from ..domain.models import EquityHolding, Position
from .screener import EntryCandidate

CONTRACT_MULTIPLIER = 100
_UNBOUNDED = 10 ** 9  # sentinel "no cap" (liquidity cap disabled or open interest unknown)


def _liquidity_cap(candidate: "EntryCandidate", sizing: EntrySizing) -> int:
    """Max contracts allowed by the strike's open interest = floor(OI * max_pct_of_oi). Fail-open
    (unbounded) when the cap is off or OI is unknown, so it never blocks on missing data."""
    pct = sizing.max_pct_of_oi
    if not pct or pct <= 0:
        return _UNBOUNDED
    oi = candidate.open_interest
    if oi is None or oi <= 0:
        return _UNBOUNDED
    return max(0, math.floor(oi * pct))


@dataclass
class ApprovedEntry:
    candidate: EntryCandidate
    contracts: int
    collateral: float  # strike * 100 * contracts


@dataclass
class SizingResult:
    """Full disposition of a sizing pass: what was approved, and why each rejection lost.

    ``rejected`` is (candidate, reason) so the scanner can persist negative examples — the
    'a candidate showed up but never entered' cases — with the exact gate that stopped them.
    """
    approved: list[ApprovedEntry]
    rejected: list[tuple[EntryCandidate, str]]


def _is_open_csp(p: Position) -> bool:
    return p.direction is Direction.SHORT and p.option_type is OptionType.PUT


class RiskSizer:
    def __init__(self, sizing: EntrySizing):
        self.sizing = sizing

    def approve(
        self,
        candidates: list[EntryCandidate],
        *,
        buying_power: float,
        account_value: float,
        open_positions: list[Position],
    ) -> list[ApprovedEntry]:
        return self.evaluate(
            candidates, buying_power=buying_power, account_value=account_value,
            open_positions=open_positions,
        ).approved

    def evaluate(
        self,
        candidates: list[EntryCandidate],
        *,
        buying_power: float,
        account_value: float,
        open_positions: list[Position],
    ) -> SizingResult:
        """Size candidates and report the full disposition (approved + reasoned rejections).

        Identical approval logic to the pre-existing ``approve``; it additionally records why
        each non-approved candidate lost, so the scanner can persist negative examples.
        """
        s = self.sizing
        held_underlyings = {p.underlying for p in open_positions}
        existing_collateral = sum(
            p.strike * CONTRACT_MULTIPLIER * p.quantity
            for p in open_positions if _is_open_csp(p)
        )
        current_csp_count = sum(1 for p in open_positions if _is_open_csp(p))

        usable_bp = buying_power * (1 - s.buying_power_reserve_pct)
        total_allowed = account_value * s.total_bp_utilization_target
        run_budget = max(0.0, min(usable_bp, total_allowed - existing_collateral))
        # Scale-invariant sizing (target_positions set): spread committed collateral across ~N names,
        # so the per-name budget shrinks as capital grows. ``diversified_cap`` is the target per-name
        # slice; ``backstop_cap`` (max_position_size_pct) only binds at small accounts. Legacy
        # (target_positions None): the per-name cap is simply max_position_size_pct of account value.
        backstop_cap = account_value * s.max_position_size_pct
        diversified_cap = total_allowed / max(1, s.target_positions) if s.target_positions else None
        position_limit = (min(s.target_positions, s.max_concurrent_positions)
                          if s.target_positions else s.max_concurrent_positions)
        slots = max(0, position_limit - current_csp_count)

        approved: list[ApprovedEntry] = []
        rejected: list[tuple[EntryCandidate, str]] = []
        seen: set[str] = set()
        for c in candidates:
            if slots <= 0:
                rejected.append((c, f"no capacity: position limit ({position_limit}) reached"))
                continue
            if run_budget <= 0:
                rejected.append((c, "no capacity: run budget exhausted"))
                continue
            if c.underlying in held_underlyings:
                rejected.append((c, f"already holding {c.underlying} (one CSP per underlying)"))
                continue
            if c.underlying in seen:
                rejected.append((c, f"{c.underlying} already approved this scan (one per name)"))
                continue
            per_contract = c.strike * CONTRACT_MULTIPLIER
            if per_contract <= 0:
                rejected.append((c, "invalid strike (<= 0)"))
                continue
            # Per-name cap: under target_positions, allow >=1 contract of an affordable name but
            # never exceed the backstop; legacy path uses the flat backstop cap.
            per_name_cap = (min(backstop_cap, max(diversified_cap, per_contract))
                            if diversified_cap is not None else backstop_cap)
            by_name = math.floor(per_name_cap / per_contract)
            by_budget = math.floor(run_budget / per_contract)
            by_liq = _liquidity_cap(c, s)
            contracts = min(by_name, by_budget, by_liq)
            if contracts < 1:
                bounds = {"per-name cap": by_name, "run budget": by_budget, "liquidity (OI)": by_liq}
                limiter = min(bounds, key=lambda k: bounds[k])
                rejected.append((c, f"too small to size: {limiter} < 1 contract "
                                    f"(collateral ${per_contract:.0f}, per-name cap "
                                    f"${per_name_cap:.0f}, budget ${run_budget:.0f})"))
                continue
            collateral = contracts * per_contract
            approved.append(ApprovedEntry(candidate=c, contracts=contracts, collateral=collateral))
            seen.add(c.underlying)
            run_budget -= collateral
            slots -= 1
        return SizingResult(approved=approved, rejected=rejected)

    def approve_covered_calls(
        self,
        candidates: list[EntryCandidate],
        *,
        holdings: list[EquityHolding],
        open_positions: list[Position],
    ) -> list[ApprovedEntry]:
        return self.evaluate_covered_calls(
            candidates, holdings=holdings, open_positions=open_positions,
        ).approved

    def evaluate_covered_calls(
        self,
        candidates: list[EntryCandidate],
        *,
        holdings: list[EquityHolding],
        open_positions: list[Position],
    ) -> SizingResult:
        """Size covered calls by shares held (no buying power — they're covered), reporting the
        full disposition.

        Sells against uncovered shares only: contracts = shares//100 minus existing short-call
        contracts on that name. One CC candidate per name per scan; respects max_concurrent_cc.
        """
        shares = {h.symbol: h.quantity for h in holdings}
        short_calls: dict[str, int] = {}
        for p in open_positions:
            if p.direction is Direction.SHORT and p.option_type is OptionType.CALL:
                short_calls[p.underlying] = short_calls.get(p.underlying, 0) + p.quantity
        slots = max(0, self.sizing.max_concurrent_cc - sum(short_calls.values()))

        approved: list[ApprovedEntry] = []
        rejected: list[tuple[EntryCandidate, str]] = []
        seen: set[str] = set()
        for c in candidates:
            if slots <= 0:
                rejected.append((c, f"no capacity: max_concurrent_cc "
                                    f"({self.sizing.max_concurrent_cc}) reached"))
                continue
            if c.underlying in seen:
                rejected.append((c, f"{c.underlying} already approved this scan (one per name)"))
                continue
            held = shares.get(c.underlying, 0)
            coverable = held // CONTRACT_MULTIPLIER - short_calls.get(c.underlying, 0)
            if coverable < 1:
                rejected.append((c, f"no uncovered shares of {c.underlying} "
                                    f"(held {held}, already-short {short_calls.get(c.underlying, 0)})"))
                continue
            approved.append(ApprovedEntry(candidate=c, contracts=coverable, collateral=0.0))
            seen.add(c.underlying)
            slots -= 1
        return SizingResult(approved=approved, rejected=rejected)
