"""ApprovalGate: human-in-the-loop for approval-gated close decisions (Phase 3).

A decision with ``requires_approval=True`` is parked in AWAITING_APPROVAL with an expiry,
and a push notification goes out carrying one-tap Approve / Reject links that hit the
control endpoints. The monitor sweeps expired requests each cycle.

  request(position, decision) -> notify with buttons, park AWAITING_APPROVAL
  approve(decision_id)        -> APPROVED, then hand to the executor (returns its Order)
  reject(decision_id)         -> REJECTED
  expire_stale()              -> any AWAITING_APPROVAL past its expiry -> EXPIRED
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from ..config import Settings
from ..domain.enums import AuditEventType, DecisionStatus
from ..domain.models import CloseDecision, Order, Position, utcnow
from ..notify.base import Notifier
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.positions import PositionStore
from .executor import OrderExecutor

log = logging.getLogger("agentic.approval")


@dataclass
class ApprovalResult:
    ok: bool
    status: str
    detail: str = ""
    order: Order | None = None


class ApprovalGate:
    def __init__(
        self,
        settings: Settings,
        decisions: DecisionStore,
        positions: PositionStore,
        executor: OrderExecutor,
        audit: AuditStore,
        notifier: Notifier | None = None,
    ):
        self.settings = settings
        self.decisions = decisions
        self.positions = positions
        self.executor = executor
        self.audit = audit
        self.notifier = notifier

    async def request(self, position: Position, decision: CloseDecision) -> None:
        """Park the decision AWAITING_APPROVAL and push an approve/reject notification."""
        expires_at = utcnow() + timedelta(seconds=self.settings.approval_timeout_seconds)
        self.decisions.mark_awaiting(decision.id, expires_at)
        base = self.settings.public_base_url
        # Per-decision token so the link authorizes only THIS close and can't be replayed from a
        # leaked decision id (buying to close a real position must not be unauthenticated).
        from ..config import close_action_token
        q = f"?t={close_action_token(decision.id) or ''}"
        actions = [
            {"action": "http", "label": "Approve",
             "url": f"{base}/control/approve/{decision.id}{q}", "method": "POST"},
            {"action": "http", "label": "Reject",
             "url": f"{base}/control/reject/{decision.id}{q}", "method": "POST"},
        ]
        title = f"Approve close? {position.underlying} {position.occ_symbol}"
        message = (
            f"{decision.rule_name}: {decision.reason}\n"
            f"qty={position.quantity} mark={position.current_mark} dte={position.dte()}\n"
            f"Approve within {self.settings.approval_timeout_seconds // 60} min "
            f"(mode={self.settings.mode})."
        )
        if self.notifier is not None:
            await self.notifier.send(title, message, priority="high", actions=actions)
        self.audit.record(
            AuditEventType.APPROVAL_REQ,
            {"rule": decision.rule_name, "occ": position.occ_symbol,
             "expires_at": expires_at.isoformat(), "approve_url": actions[0]["url"]},
            source="approval", position_id=position.id, decision_id=decision.id,
        )

    async def approve(self, decision_id: str) -> ApprovalResult:
        decision = self.decisions.get(decision_id)
        if decision is None:
            return ApprovalResult(False, "not_found", "Unknown decision id.")
        if decision.status == DecisionStatus.DONE:
            return ApprovalResult(True, "already_done", "Already executed.")
        if decision.status != DecisionStatus.AWAITING_APPROVAL:
            return ApprovalResult(False, "not_pending",
                                  f"Decision is {decision.status.value}, not awaiting approval.")
        if decision.expires_at is not None and utcnow() > decision.expires_at:
            self.decisions.set_status(decision_id, DecisionStatus.EXPIRED)
            self._audit_resp(decision, "expired")
            return ApprovalResult(False, "expired", "Approval window passed.")

        position = self.positions.get(decision.position_id)
        if position is None:
            self.decisions.set_status(decision_id, DecisionStatus.FAILED)
            return ApprovalResult(False, "no_position", "Position no longer exists.")

        self.decisions.set_status(decision_id, DecisionStatus.APPROVED)
        self._audit_resp(decision, "approved")
        order = await self.executor.execute_close(position, decision)
        return ApprovalResult(True, "approved", "Close submitted.", order=order)

    async def reject(self, decision_id: str) -> ApprovalResult:
        decision = self.decisions.get(decision_id)
        if decision is None:
            return ApprovalResult(False, "not_found", "Unknown decision id.")
        if decision.status != DecisionStatus.AWAITING_APPROVAL:
            return ApprovalResult(False, "not_pending",
                                  f"Decision is {decision.status.value}.")
        self.decisions.set_status(decision_id, DecisionStatus.REJECTED)
        self._audit_resp(decision, "rejected")
        return ApprovalResult(True, "rejected", "Close rejected.")

    def expire_stale(self) -> int:
        """Mark any past-due AWAITING_APPROVAL decisions EXPIRED. Returns how many."""
        now = utcnow()
        pending = self.decisions.list_by_status(DecisionStatus.AWAITING_APPROVAL)
        n = 0
        for d in pending:
            if d.expires_at is not None and now > d.expires_at:
                self.decisions.set_status(d.id, DecisionStatus.EXPIRED)
                self._audit_resp(d, "expired")
                n += 1
        if n:
            log.info("Expired %d stale approval request(s).", n)
        return n

    def _audit_resp(self, decision: CloseDecision, response: str) -> None:
        self.audit.record(
            AuditEventType.APPROVAL_RESP,
            {"response": response, "rule": decision.rule_name},
            source="approval", position_id=decision.position_id, decision_id=decision.id,
        )
