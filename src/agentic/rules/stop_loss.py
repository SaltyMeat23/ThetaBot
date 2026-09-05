"""Stop-loss rule for short premium — cap a losing position / avoid deep assignment.

Two independent triggers (either fires a close, whichever hits first):

  * ``loss_mult``: cost-to-close has risen to N× the credit received (e.g. 2.0 = the option now
    costs twice what we sold it for, i.e. ~-100% of credit).
  * ``delta_stop``: the option's |delta| has crossed a threshold (e.g. 0.50) — the strike is being
    breached and assignment risk is climbing.

Stateless. For the wheel, assignment is acceptable, so this is a deliberate opt-in defense against
a name that "keeps going down". Uses the conservative ask as cost-to-close.
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import RuleType
from ..domain.models import CloseDecision, Position
from ..marketdata.quote import OptionQuote
from .base import Rule


class StopLossRule(Rule):
    rule_type = RuleType.STOP_LOSS

    def evaluate(
        self, position: Position, quote: OptionQuote | None, now: datetime
    ) -> CloseDecision | None:
        if position.credit_received <= 0 or quote is None or not quote.is_valid:
            return None
        cost = quote.ask if quote.ask is not None else quote.midpoint
        if cost is None:
            return None

        loss_mult = self.params.get("loss_mult")
        if loss_mult is not None and cost >= float(loss_mult) * position.credit_received:
            loss_pct = (cost / position.credit_received - 1) * 100
            reason = (f"Stop-loss: cost ${cost:.2f} >= {float(loss_mult):.1f}x credit "
                      f"${position.credit_received:.2f} (~-{loss_pct:.0f}% of credit).")
            return self._decision(position, reason, now)

        delta_stop = self.params.get("delta_stop")
        d = quote.delta if quote.delta is not None else position.delta
        if delta_stop is not None and d is not None and abs(d) >= float(delta_stop):
            reason = (f"Delta stop: |delta| {abs(d):.2f} >= {float(delta_stop):.2f} "
                      f"(strike being breached).")
            return self._decision(position, reason, now)
        return None
