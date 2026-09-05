"""Profit-target rule for short premium (covered calls / cash-secured puts).

We collected ``credit_received`` per contract at open and capture profit as the option decays.
Two modes via params:

  * Fixed (default): close once captured profit >= ``profit_pct`` (e.g. 0.5 = keep 50% of premium).
  * Trailing (``trailing: true``): arm once profit reaches ``profit_pct``, then let winners run —
    close only when profit pulls back ``trail_gap`` from its peak (high-water mark tracked on the
    position), with the exit floored at the arm level so it never books less than the arm profit
    on a normal retracement. A single-poll gap-reversal can still exit slightly below the floor —
    the inherent trade-off of trailing vs. a fixed take.

Uses the (conservative) ask as cost-to-close when available, else the midpoint; a valid,
non-stale quote is required.
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import RuleType
from ..domain.models import CloseDecision, Position
from ..marketdata.quote import OptionQuote
from .base import Rule, cost_to_close, profit_captured


class ProfitTargetRule(Rule):
    rule_type = RuleType.PROFIT_TARGET

    def evaluate(
        self, position: Position, quote: OptionQuote | None, now: datetime
    ) -> CloseDecision | None:
        cost = cost_to_close(quote)
        cur = profit_captured(position, cost)
        if cur is None:
            return None
        arm = float(self.params.get("profit_pct", 0.5))

        if not self.params.get("trailing", False):
            if cur >= arm:
                reason = (f"Profit target: captured ~{cur * 100:.0f}% "
                          f">= {arm:.0%} (cost ${cost:.2f}).")
                return self._decision(position, reason, now)
            return None

        # Trailing: arm at profit_pct, then exit on a trail_gap pullback from the peak.
        gap = float(self.params.get("trail_gap", 0.20))
        peak = max(position.peak_profit_pct or 0.0, cur)
        if peak < arm:
            return None  # not yet armed — let it keep working
        exit_level = max(arm, peak - gap)
        if cur <= exit_level:
            reason = (f"Trailing take-profit: captured ~{cur * 100:.0f}% pulled back to exit "
                      f"{exit_level * 100:.0f}% (peak {peak * 100:.0f}%, arm {arm:.0%}).")
            return self._decision(position, reason, now)
        return None
