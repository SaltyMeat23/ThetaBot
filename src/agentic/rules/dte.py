"""Days-to-expiration rule: close or alert as expiration approaches.

Gamma/assignment risk rises sharply in the final weeks; the common wheel/CSP/CC practice
is to close or roll at ~21 DTE. ``action`` is honored: ``alert`` produces a notify-only
decision (a heads-up that NEVER routes to the executor, so it can't buy back a tested put at
a loss — the roll manager / assignment handle that), while ``close`` produces an executable
decision routed like any other auto/approval rule.
"""
from __future__ import annotations

from datetime import datetime

from ..domain.enums import RuleType
from ..domain.models import CloseDecision, Position
from ..marketdata.quote import OptionQuote
from .base import Rule


class DteRule(Rule):
    rule_type = RuleType.DTE

    def evaluate(
        self, position: Position, quote: OptionQuote | None, now: datetime
    ) -> CloseDecision | None:
        threshold = int(self.params.get("dte_threshold", 21))
        action = self.params.get("action", "alert")
        dte = position.dte(now.date())
        if dte <= threshold:
            reason = (
                f"DTE {dte} <= {threshold} (expires {position.expiration.isoformat()}); "
                f"action={action}."
            )
            # action=alert is a heads-up only; only action=close is allowed to auto-execute.
            return self._decision(position, reason, now, notify_only=(action != "close"))
        return None
