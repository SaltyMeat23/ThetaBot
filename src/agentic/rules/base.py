"""Rule interface + shared helpers.

A Rule inspects one (position, quote, now) and returns a CloseDecision when its
condition is met, else None. Rules are pure and side-effect-free — persistence,
dedup, notification and execution are the monitor's job.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..domain.enums import RuleType
from ..domain.models import CloseDecision, Position, utcnow
from ..marketdata.quote import OptionQuote


def dedup_key(position_id: str, rule_type: RuleType, now: datetime | None = None) -> str:
    """One decision per position+rule_type+calendar-day (UTC)."""
    day = (now or utcnow()).date().isoformat()
    return f"{position_id}:{rule_type.value}:{day}"


def cost_to_close(quote: OptionQuote | None) -> float | None:
    """Conservative cost to buy back a short: the ask, falling back to the midpoint.
    None when there's no usable quote."""
    if quote is None or not quote.is_valid:
        return None
    return quote.ask if quote.ask is not None else quote.midpoint


def profit_captured(position: Position, cost: float | None) -> float | None:
    """Fraction of the collected credit currently captured (1.0 = worthless = full profit,
    negative = a loss). None when not computable."""
    if position.credit_received <= 0 or cost is None:
        return None
    return 1.0 - cost / position.credit_received


class Rule(ABC):
    rule_type: RuleType

    def __init__(self, name: str, requires_approval: bool, params: dict):
        self.name = name
        self.requires_approval = requires_approval
        self.params = params

    @abstractmethod
    def evaluate(
        self, position: Position, quote: OptionQuote | None, now: datetime
    ) -> CloseDecision | None:
        raise NotImplementedError

    def _decision(self, position: Position, reason: str, now: datetime,
                  *, notify_only: bool = False) -> CloseDecision:
        return CloseDecision(
            position_id=position.id,
            rule_name=self.name,
            rule_type=self.rule_type,
            reason=reason,
            requires_approval=self.requires_approval,
            dedup_key=dedup_key(position.id, self.rule_type, now),
            notify_only=notify_only,
        )
