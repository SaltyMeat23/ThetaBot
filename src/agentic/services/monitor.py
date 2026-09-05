"""Monitor loop: poll broker positions, refresh quotes, (Phase 1+) evaluate rules.

Phase 0 scope: read-only. Each cycle it pulls open positions from the broker (source of
truth), enriches each with a fresh market-data quote, upserts into the position store,
and records a POLL audit event. The rules engine / executor hooks are wired but optional
so later phases plug in without restructuring the loop.
"""
from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..domain.enums import AuditEventType
from ..marketdata.base import MarketDataProvider
from ..brokers.base import ExecutionBroker
from ..notify.base import Notifier
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.positions import PositionStore
from ..rules.base import cost_to_close, profit_captured
from .killswitch import KillSwitch
from .market_hours import is_market_hours

log = logging.getLogger("agentic.monitor")


class MonitorLoop:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        positions: PositionStore,
        audit: AuditStore,
        killswitch: KillSwitch,
        rules_engine=None,        # set in Phase 1
        decisions: DecisionStore | None = None,  # set in Phase 1
        notifier: Notifier | None = None,        # set in Phase 1
        executor=None,            # set in Phase 2
        signal_processor=None,    # set in Phase 3
        approval_gate=None,       # set in Phase 3
        roll_manager=None,        # tested-put roll defense
    ):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.positions = positions
        self.audit = audit
        self.killswitch = killswitch
        self.rules_engine = rules_engine
        self.decisions = decisions
        self.notifier = notifier
        self.executor = executor
        self.signal_processor = signal_processor
        self.approval_gate = approval_gate
        self.roll_manager = roll_manager
        self._stop = asyncio.Event()

    async def run(self) -> None:
        log.info("Monitor loop started (mode=%s, broker=%s).",
                 self.settings.mode, self.broker.capabilities().name)
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.killswitch.record_success()
            except Exception as exc:  # noqa: BLE001 — a bad cycle must not kill the loop
                log.exception("Monitor cycle error: %s", exc)
                self.audit.record(AuditEventType.ERROR, {"where": "monitor", "error": str(exc)})
                self.killswitch.record_broker_error("monitor")
            delay = (
                self.settings.poll_interval_seconds
                if is_market_hours()
                else self.settings.poll_interval_closed_seconds
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """One monitor cycle. Returns the number of open positions seen."""
        # Pick up any live rule edits (settings API) without a restart.
        if self.rules_engine is not None:
            self.rules_engine.refresh(self.settings.rules)
        broker_positions = await self.broker.get_open_positions()
        _caps = getattr(self.broker, "capabilities", None)
        src_is_paper = _caps().is_paper if _caps is not None else None  # tag positions' source
        # Rule evaluation and rolls only run during the regular session: after hours nothing is
        # actionable (orders can't fill, quotes are stale), so evaluating would only emit alerts
        # you can't act on — and repeat them every closed-market poll. Position sync still runs
        # around the clock so the dashboard and reconcile stay current.
        market_open = is_market_hours()
        for pos in broker_positions:
            quote = await self.market_data.get_quote(pos)
            if quote is not None:
                pos.current_bid = quote.bid
                pos.current_ask = quote.ask
                pos.current_mark = quote.mark
                if quote.delta is not None:
                    pos.delta = quote.delta
                if quote.iv is not None:
                    pos.iv = quote.iv
            # High- and low-water marks of captured profit: MFE (for trailing take-profit exits) and
            # MAE (worst drawdown, journaled at resolution to study exit timing).
            cur = profit_captured(pos, cost_to_close(quote))
            if cur is not None:
                pos.peak_profit_pct = max(pos.peak_profit_pct or 0.0, cur)
                pos.trough_profit_pct = min(pos.trough_profit_pct or 0.0, cur)
            pos.is_paper = src_is_paper
            self.positions.upsert(pos)

            # Phase 1+: evaluate rules; persist new decisions and notify (regular session only).
            if market_open and self.rules_engine is not None and self.decisions is not None:
                await self._evaluate(pos, quote)
            # Roll defense: a tested put near expiry is rolled out-and-down for a credit rather
            # than stopped out or assigned at a bad basis (no-op unless roll.enabled + tested).
            if market_open and self.roll_manager is not None:
                await self.roll_manager.try_roll(pos, quote)

        self.audit.record(
            AuditEventType.POLL,
            {"open_positions": len(broker_positions),
             "symbols": [p.occ_symbol for p in broker_positions],
             "paused": self.killswitch.is_paused()},
            source="monitor",
        )

        # Phase 3: drain queued TradingView signals and expire stale approval requests.
        if self.signal_processor is not None:
            await self.signal_processor.process_pending()
        if self.approval_gate is not None:
            self.approval_gate.expire_stale()

        log.info("Polled %d open position(s).", len(broker_positions))
        return len(broker_positions)

    async def _evaluate(self, pos, quote) -> None:
        """Evaluate rules for one position. New decisions are persisted (dedup'd),
        audited, and notified. Phase 1 NEVER executes — even auto rules only notify;
        Phase 2 plugs the executor in here for requires_approval=false decisions.
        """
        decisions = self.rules_engine.evaluate(pos, quote)
        for decision in decisions:
            if not self.decisions.insert_if_new(decision):
                continue  # already decided this position+rule today
            self.audit.record(
                AuditEventType.DECISION,
                {"rule": decision.rule_name, "reason": decision.reason,
                 "requires_approval": decision.requires_approval,
                 "occ": pos.occ_symbol},
                source="monitor",
                position_id=pos.id,
                decision_id=decision.id,
            )
            # Hybrid autonomy: auto rules (requires_approval=false) go straight to the executor,
            # which owns its own audit + notify; approval-gated decisions are notified here and
            # picked up by the Phase 3 approval gate. A notify_only decision (e.g. a DTE alert)
            # NEVER executes regardless of requires_approval — it must not buy back a tested put at
            # a loss; the roll manager or assignment handles a position moving against us.
            if self.executor is not None and not decision.requires_approval and not decision.notify_only:
                await self.executor.execute_close(pos, decision, quote)
            else:
                await self._notify_decision(pos, decision)

    async def _notify_decision(self, pos, decision) -> None:
        if self.notifier is None:
            return
        if decision.notify_only:
            gate = "ALERT"
        elif decision.requires_approval:
            gate = "NEEDS APPROVAL"
        else:
            gate = "AUTO"
        title = f"Close signal: {pos.underlying} {pos.occ_symbol}"
        message = (
            f"[{gate}] {decision.rule_name}\n{decision.reason}\n"
            f"qty={pos.quantity} mark={pos.current_mark} dte={pos.dte()} "
            f"(mode={self.settings.mode}, no order placed in Phase 1)"
        )
        priority = "high" if (not decision.requires_approval and not decision.notify_only) else "normal"
        await self.notifier.send(title, message, priority=priority)
        self.audit.record(
            AuditEventType.APPROVAL_REQ if decision.requires_approval else AuditEventType.DECISION,
            {"notified": True, "rule": decision.rule_name},
            source="monitor", position_id=pos.id, decision_id=decision.id,
        )

    def stop(self) -> None:
        self._stop.set()
