"""Reconciliation loop: keep the local store consistent with broker reality.

Each cycle (and once at startup) it diffs the broker against the store and repairs drift:
  * Discovered  — at broker, not OPEN in store -> insert (how manually-opened positions
                  are picked up, since opening is out of scope).
  * Gone        — OPEN in store, absent at broker -> classify EXPIRED (past expiration) vs
                  CLOSED (closed before expiry). ASSIGNED needs a broker assignment record;
                  left as a TODO hook (still recorded as CLOSED for now, flagged in audit).
  * Qty drift   — present both sides, quantity differs -> external partial close; update.
  * Orphans     — orders left SUBMITTED/PARTIAL -> refresh from broker and finalize; a
                  late FILL also closes its position.

Runs on its own slower cadence; safe to run while the monitor runs (single asyncio loop).
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..brokers.base import ExecutionBroker
from ..errors import describe_exception
from ..domain.enums import AuditEventType, DecisionStatus, OptionType, OrderStatus, PositionStatus
from ..domain.models import utcnow
from ..notify.base import Notifier
from ..store.audit import AuditStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.orders import OrderStore
from ..store.positions import PositionStore
from ..store.trade_journal import TradeJournalStore
from .killswitch import KillSwitch
from .stats import filled_close, position_pnl

log = logging.getLogger("agentic.reconcile")


class ReconcileLoop:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        positions: PositionStore,
        audit: AuditStore,
        orders: OrderStore | None = None,
        killswitch: KillSwitch | None = None,
        notifier: Notifier | None = None,
        trade_journal: TradeJournalStore | None = None,
        entry_decisions: EntryDecisionStore | None = None,
    ):
        self.settings = settings
        self.broker = broker
        self.positions = positions
        self.audit = audit
        self.orders = orders
        self.killswitch = killswitch
        self.notifier = notifier
        self.trade_journal = trade_journal
        self.entry_decisions = entry_decisions
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("Reconcile loop started.")
        await self.run_once()  # reconcile immediately at startup
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.reconcile_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
            if self._stop.is_set():
                break
            try:
                await self.run_once()
                if self.killswitch is not None:
                    self.killswitch.record_success()
            except Exception as exc:  # noqa: BLE001
                log.exception("Reconcile cycle error: %s", exc)
                self.audit.record(
                    AuditEventType.ERROR,
                    {"where": "reconcile", **describe_exception(exc)},
                )
                if self.killswitch is not None:
                    self.killswitch.record_broker_error("reconcile")

    async def run_once(self) -> dict:
        # Degraded guard: if we're live-armed but fell back to the paper simulator, the broker's
        # positions are the SIMULATOR's, not the real account. Syncing would mark every real
        # position "gone -> closed" and insert paper positions as real (the ONDS corruption).
        # Skip all sync until the real broker is restored.
        from ..brokers.factory import broker_degraded
        if broker_degraded(self.settings, self.broker):
            diff = {"degraded": True, "note": "broker on paper while live — sync skipped to protect "
                    "real positions"}
            self.audit.record(AuditEventType.RECONCILE, diff, source="reconcile")
            log.warning("Reconcile skipped: %s", diff["note"])
            return diff

        broker_positions = await self.broker.get_open_positions()
        broker_by_occ = {p.occ_symbol: p for p in broker_positions}
        store_open = {p.occ_symbol: p for p in self.positions.list_open()}

        discovered = [occ for occ in broker_by_occ if occ not in store_open]
        gone = [occ for occ in store_open if occ not in broker_by_occ]
        qty_drift: list[dict] = []

        is_paper = self.broker.capabilities().is_paper  # stamp the source broker onto discoveries

        # Discovered -> insert.
        for occ in discovered:
            p = broker_by_occ[occ]
            p.is_paper = is_paper
            self.positions.upsert(p)

        # Present both sides -> check quantity drift (external partial close).
        for occ, bpos in broker_by_occ.items():
            spos = store_open.get(occ)
            if spos is not None and spos.quantity != bpos.quantity:
                qty_drift.append({"occ": occ, "from": spos.quantity, "to": bpos.quantity})
                bpos.is_paper = is_paper
                self.positions.upsert(bpos)  # upsert syncs quantity

        # Snapshot equity holdings to detect assignment (a short put gone + shares appearing).
        equity_by_symbol: dict[str, int] = {}
        try:
            for h in await self.broker.get_equity_positions():
                equity_by_symbol[h.symbol] = h.quantity
        except Exception as exc:  # noqa: BLE001 — equity read is best-effort
            log.warning("equity snapshot failed during reconcile: %s", exc)

        # Gone -> classify EXPIRED / ASSIGNED / called-away / CLOSED, and notify on the wheel
        # hand-offs (assignment + called-away). Plain expiry is routine (esp. weeklies) -> quiet.
        classified: list[dict] = []
        for occ in gone:
            spos = store_open[occ]
            notify: tuple[str, str] | None = None
            if spos.dte() < 0:
                status, j_status = PositionStatus.EXPIRED, "expired"
            elif spos.option_type is OptionType.PUT and equity_by_symbol.get(spos.underlying):
                # Short put vanished before expiry and we now hold the shares -> assigned.
                status, j_status = PositionStatus.ASSIGNED, "assigned"
                notify = (
                    f"CSP ASSIGNED: {spos.underlying}",
                    f"{spos.occ_symbol} assigned — you now hold "
                    f"{equity_by_symbol[spos.underlying]} sh of {spos.underlying}. "
                    f"A covered call becomes a candidate next scan.",
                )
            elif spos.option_type is OptionType.CALL:
                # Short call vanished before expiry -> likely called away (shares sold).
                status, j_status = PositionStatus.CLOSED, "called_away"
                notify = (
                    f"Covered call gone: {spos.underlying}",
                    f"{spos.occ_symbol} closed before expiry — likely called away "
                    f"(shares sold). Wheel returns to selling puts.",
                )
            else:
                status, j_status = PositionStatus.CLOSED, "closed"
            self.positions.set_status(spos.id, status)
            self._journal_close(spos, j_status)
            entry = {"occ": occ, "status": status.value}
            if status is PositionStatus.ASSIGNED:
                entry["assigned"] = True
            classified.append(entry)
            if notify is not None:
                await self._notify(*notify)

        orphans = await self._finalize_orphan_orders()
        healed = self._heal_entry_decisions(broker_by_occ)

        diff = {
            "broker_open": len(broker_by_occ),
            "store_open": len(store_open),
            "discovered": discovered,
            "gone": classified,
            "qty_drift": qty_drift,
            "orphans_finalized": orphans,
            "entry_decisions_healed": healed,
        }
        self.audit.record(AuditEventType.RECONCILE, diff, source="reconcile")
        if discovered or classified or qty_drift or orphans or healed:
            log.info("Reconcile: %s", diff)
        return diff

    # Entry decisions that the executor marked FAILED/EXECUTING can be false negatives: a
    # sub-second fill whose order-state lagged (RH MCP), so the decision was left non-DONE while
    # the short actually opened. The broker position read is authoritative — if the contract is
    # open at the broker, that entry filled; heal the decision to DONE so the labeled dataset
    # (and premium-collected accounting) reflect reality instead of a phantom failure.
    _HEALABLE = (DecisionStatus.FAILED, DecisionStatus.EXECUTING)

    def _heal_entry_decisions(self, broker_by_occ: dict) -> list[dict]:
        if self.entry_decisions is None:
            return []
        healed: list[dict] = []
        for occ in broker_by_occ:
            d = self.entry_decisions.most_recent_by_occ(occ)
            if d is None or d.status not in self._HEALABLE:
                continue
            self.entry_decisions.set_status(d.id, DecisionStatus.DONE)
            healed.append({"occ": occ, "decision_id": d.id, "from": d.status.value})
            log.info("Healed entry decision %s for %s: %s -> DONE (open at broker).",
                     d.id, occ, d.status.value)
        return healed

    async def _finalize_orphan_orders(self) -> list[dict]:
        """Refresh any orders stuck SUBMITTED/PARTIAL and apply their terminal state."""
        if self.orders is None:
            return []
        finalized: list[dict] = []
        for order in self.orders.list_by_status(OrderStatus.SUBMITTED, OrderStatus.PARTIAL):
            try:
                refreshed = await self.broker.get_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("Orphan order refresh failed for %s: %s", order.client_order_id, exc)
                continue
            if refreshed.status == order.status:
                continue
            refreshed.last_status_at = utcnow()
            self.orders.update(refreshed)
            finalized.append({"client_order_id": refreshed.client_order_id,
                              "status": refreshed.status.value})
            if refreshed.status == OrderStatus.FILLED:
                self.positions.set_status(refreshed.position_id, PositionStatus.CLOSED)
        return finalized

    def _journal_close(self, spos, j_status: str) -> None:
        """Backfill the open trade-journal row when a position resolves externally.

        Realized P&L is computed for both expiry (full credit kept) and a close/called-away
        before expiry — the latter from the filled buy-to-close order if we captured it, else
        estimated from the last mark (position_pnl fallback). Assignment is left P&L-less (it
        depends on how the assigned shares are later disposed).
        """
        if self.trade_journal is None:
            return
        je = self.trade_journal.find_open_by_occ(spos.occ_symbol)
        if je is None:
            return
        realized = close_price = None
        if j_status == "expired":
            spos.status = PositionStatus.EXPIRED  # full credit kept
            realized = position_pnl(spos, None)["realized_pnl"]
        elif j_status in ("closed", "called_away"):
            spos.status = PositionStatus.CLOSED  # so position_pnl takes the realized branch
            close_order = (filled_close(self.orders.list_by_position(spos.id))
                           if self.orders is not None else None)
            info = position_pnl(spos, close_order)
            realized, close_price = info["realized_pnl"], info["close_price"]
        self.trade_journal.set_outcome(
            je.id, status=j_status, realized_pnl=realized, close_price=close_price,
            exit_reason=f"reconcile:{j_status}", entered_at=je.entered_at,
            mfe_pct=spos.peak_profit_pct, mae_pct=spos.trough_profit_pct,
        )

    async def _notify(self, title: str, message: str) -> None:
        if self.notifier is not None:
            try:
                await self.notifier.send(title, message, priority="high")
            except Exception as exc:  # noqa: BLE001 — notification must not break reconcile
                log.warning("reconcile notify failed: %s", exc)

    def stop(self) -> None:
        self._stop.set()
