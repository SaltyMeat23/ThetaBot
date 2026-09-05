"""OrderExecutor: turn an auto-approved CloseDecision into a filled buy-to-close.

Safety pipeline (every order goes through all of it):
  1. Kill-switch recheck    — refuse if paused since the decision was made.
  2. Live-arming gate        — a non-paper broker only places real orders when
                               ``settings.is_live`` (mode=live AND i_understand_live_trading).
  3. Fresh-quote stale guard — reject missing / invalid / stale quotes (no blind orders).
  4. Limit-price calc        — mid + buffer, capped at ask*(1+slippage_cap), tick-rounded.
  5. Persist-before-submit   — write the Order PENDING keyed by a deterministic
                               client_order_id; reuse it on retry (idempotency).
  6. Submit + await fill     — poll to fill_timeout; bounded re-price on no-fill/partial.
  7. Settle                  — on FILLED mark Position CLOSED + Decision DONE; audit + notify.

The limit math is a free function so it can be unit-tested without a broker.
"""
from __future__ import annotations

import asyncio
import logging
import time

from ..config import Settings
from ..brokers.base import ExecutionBroker
from ..errors import describe_exception
from ..domain.enums import AuditEventType, DecisionStatus, OrderStatus, PositionStatus
from ..domain.models import CloseDecision, EntryDecision, Order, Position, utcnow
from ..marketdata.base import MarketDataProvider
from ..marketdata.quote import OptionContractQuote, OptionQuote
from ..notify.base import Notifier
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.orders import OrderStore
from ..store.positions import PositionStore
from ..store.trade_journal import TradeJournalStore
from .killswitch import KillSwitch
from .stats import position_pnl

log = logging.getLogger("agentic.executor")


def round_to_tick(price: float, tick: float = 0.01) -> float:
    """Round to the nearest option price tick (default 1 cent)."""
    if tick <= 0:
        return round(price, 2)
    return round(round(price / tick) * tick, 4)


def compute_limit_price(
    quote: OptionQuote,
    *,
    buffer_pct: float,
    slippage_cap_pct: float,
    tick: float = 0.01,
) -> float | None:
    """Buy-to-close limit price: midpoint nudged up by ``buffer_pct`` to improve fill odds,
    but never above ``ask * (1 + slippage_cap_pct)``. Returns None if the quote can't price.

    Buying back a short, we pay a debit, so we bias *up* from mid (a higher limit fills more
    readily) while the slippage cap bounds how much we'll overpay versus the current ask.
    """
    mid = quote.midpoint
    if mid is None or mid <= 0:
        return None
    target = mid * (1 + buffer_pct)
    if quote.ask is not None and quote.ask > 0:
        cap = quote.ask * (1 + slippage_cap_pct)
        target = min(target, cap)
    return round_to_tick(target, tick)


def compute_open_limit_price(
    quote: OptionContractQuote,
    *,
    buffer_pct: float,
    slippage_cap_pct: float,
    tick: float = 0.01,
) -> float | None:
    """Sell-to-open limit price: midpoint nudged *down* by ``buffer_pct`` toward the bid to
    improve fill odds, but never below ``bid * (1 - slippage_cap_pct)`` so we don't dump credit.
    Selling a short put we collect a credit, so a lower limit fills more readily.
    """
    mid = quote.midpoint
    if mid is None or mid <= 0:
        return None
    target = mid * (1 - buffer_pct)
    if quote.bid is not None and quote.bid > 0:
        floor = quote.bid * (1 - slippage_cap_pct)
        target = max(target, floor)
    return round_to_tick(target, tick)


class OrderExecutor:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        positions: PositionStore,
        orders: OrderStore,
        decisions: DecisionStore,
        audit: AuditStore,
        killswitch: KillSwitch,
        notifier: Notifier | None = None,
        entry_decisions: EntryDecisionStore | None = None,
        trade_journal: TradeJournalStore | None = None,
        *,
        poll_interval_seconds: float = 2.0,
    ):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.positions = positions
        self.orders = orders
        self.decisions = decisions
        self.audit = audit
        self.killswitch = killswitch
        self.notifier = notifier
        self.entry_decisions = entry_decisions
        self.trade_journal = trade_journal
        self.poll_interval = poll_interval_seconds

    async def execute_close(
        self, position: Position, decision: CloseDecision, quote: OptionQuote | None = None
    ) -> Order | None:
        """Execute a buy-to-close for ``position`` per ``decision``. Returns the final Order,
        or None if blocked (paused / not armed / unusable quote / nothing to do)."""
        # 1. Kill-switch recheck (state may have changed since the decision was made).
        if self.killswitch.is_paused():
            await self._block(position, decision, "killswitch engaged; order suppressed")
            return None

        # 3. Fresh quote + stale guard (refetch unless the caller passed a fresh one).
        if quote is None:
            quote = await self.market_data.get_quote(position)
        bad = self._quote_problem(quote)
        if bad:
            await self._block(position, decision, f"unusable quote: {bad}")
            return None

        # 4. Limit price.
        ex = self.settings.execution
        limit = compute_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct
        )
        if limit is None:
            await self._block(position, decision, "could not compute a limit price")
            return None

        # 2. Live-arming gate.
        is_paper = self.broker.capabilities().is_paper
        if not is_paper and not self.settings.is_live:
            await self._block(
                position, decision,
                f"NOT ARMED (mode={self.settings.mode}, i_understand_live_trading="
                f"{self.settings.i_understand_live_trading}); would buy-to-close "
                f"{position.quantity}x {position.occ_symbol} @ {limit:.2f}",
                status=DecisionStatus.PROPOSED,  # leave it actionable once armed
            )
            return None

        # 5. Persist-before-submit (idempotent on a deterministic client_order_id).
        order = Order(
            decision_id=decision.id,
            position_id=position.id,
            occ_symbol=position.occ_symbol,
            option_id=position.option_id,
            quantity=position.quantity,
            limit_price=limit,
            is_paper=is_paper,
            client_order_id=f"close-{decision.id}",
        )
        if not self.orders.insert_if_new(order):
            existing = self.orders.get_by_client_order_id(order.client_order_id)
            if existing is not None:
                if existing.status == OrderStatus.FILLED:
                    return existing  # already done; never double-submit
                order = existing     # resume an in-flight order

        self.decisions.set_status(decision.id, DecisionStatus.EXECUTING)
        self.positions.set_status(position.id, PositionStatus.CLOSING)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"occ": order.occ_symbol, "qty": order.quantity, "limit": order.limit_price,
             "is_paper": order.is_paper, "client_order_id": order.client_order_id},
            source="executor", position_id=position.id,
            decision_id=decision.id, order_id=order.id,
        )

        # 6. Submit + await fill.
        try:
            order.submitted_at = order.submitted_at or utcnow()
            submitted = await self.broker.submit_close_order(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("submit_close_order failed: %s", exc)
            order.status = OrderStatus.REJECTED
            order.last_status_at = utcnow()
            self.orders.update(order)
            self.decisions.set_status(decision.id, DecisionStatus.FAILED)
            self.audit.record(
                AuditEventType.ERROR, {"where": "executor.submit", **describe_exception(exc)},
                source="executor", position_id=position.id,
                decision_id=decision.id, order_id=order.id,
            )
            await self._notify("Close FAILED", f"{position.occ_symbol}: submit error: {exc}",
                               priority="high")
            return order

        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        final = await self._await_fill(submitted, position)

        # 7. Settle.
        if final.status == OrderStatus.FILLED:
            self.positions.set_status(position.id, PositionStatus.CLOSED)
            self.decisions.set_status(decision.id, DecisionStatus.DONE)
            self.audit.record(
                AuditEventType.ORDER_FILL,
                {"occ": final.occ_symbol, "filled_qty": final.filled_qty,
                 "avg_fill_price": final.avg_fill_price, "is_paper": final.is_paper},
                source="executor", position_id=position.id,
                decision_id=decision.id, order_id=final.id,
            )
            self._journal_outcome(position, final, decision.rule_name)
            tag = "(paper)" if final.is_paper else "(LIVE)"
            await self._notify(
                f"Closed {position.underlying} {tag}",
                f"{decision.rule_name}: bought to close {final.filled_qty}x "
                f"{position.occ_symbol} @ {final.avg_fill_price}",
                priority="high",
            )
        else:
            self.decisions.set_status(decision.id, DecisionStatus.FAILED)
            await self._notify(
                "Close did not fill",
                f"{position.occ_symbol}: order {final.status.value} after "
                f"{ex.fill_timeout_seconds}s (broker_id={final.broker_order_id}).",
                priority="high",
            )
        return final

    # ------------------------------------------------------------------ open (entry)
    async def execute_open(
        self, decision: EntryDecision, quote: OptionContractQuote
    ) -> Order | None:
        """Execute a sell-to-open CSP for ``decision`` using the fresh contract ``quote``.

        Same safety pipeline as execute_close, plus a real-time-feed gate: live entry is
        refused on a delayed feed. The opened position is NOT created here — reconcile
        discovers it from the broker and the existing close rules then manage it.
        """
        if self.entry_decisions is None:
            raise RuntimeError("execute_open requires an EntryDecisionStore.")

        # 1. Kill switch.
        if self.killswitch.is_paused():
            await self._block_entry(decision, "killswitch engaged; entry suppressed")
            return None

        # Refetch a fresh quote if the caller's has gone stale. The AI review can add several
        # seconds between the scan-time fetch and here, ageing the quote past the stale guard — an
        # approved entry must not die just because analysis took a moment. Execution uses the
        # freshest quote available; if the refetch fails we fall through to the guard below.
        if quote is None or quote.is_stale(self.settings.max_quote_age_seconds):
            try:
                chain = await self.market_data.get_chain(decision.underlying)
                fresh = next(
                    (c for c in chain if c.occ_symbol == decision.occ_symbol), None)
                if fresh is not None:
                    quote = fresh
            except Exception as exc:  # noqa: BLE001 — the stale guard below will block if needed
                log.warning("execute_open quote refetch failed for %s: %s",
                            decision.occ_symbol, exc)

        # 3. Quote stale/validity guard.
        bad = self._open_quote_problem(quote)
        if bad:
            await self._block_entry(decision, f"unusable quote: {bad}")
            return None

        # 4. Limit price (credit).
        ex = self.settings.execution
        limit = compute_open_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct, slippage_cap_pct=ex.slippage_cap_pct
        )
        if limit is None:
            await self._block_entry(decision, "could not compute a limit price")
            return None

        is_paper = self.broker.capabilities().is_paper
        # 2. Live-arming gate.
        if not is_paper and not self.settings.is_live:
            await self._block_entry(
                decision,
                f"NOT ARMED (mode={self.settings.mode}); would sell-to-open "
                f"{decision.contracts}x {decision.occ_symbol} @ {limit:.2f}",
                status=DecisionStatus.PROPOSED,
            )
            return None
        # 2b. Real-time feed gate — never auto-enter live off a delayed feed.
        if not is_paper and self.settings.is_live and not getattr(
            self.market_data, "is_realtime", False
        ):
            await self._block_entry(
                decision, "live entry requires a real-time (OPRA) feed; refusing on delayed data"
            )
            return None

        # 5. Persist-before-submit (idempotent). No position yet — reconcile creates it.
        order = Order(
            decision_id=decision.id,
            position_id="",
            occ_symbol=decision.occ_symbol,
            option_id=decision.option_id,
            quantity=decision.contracts,
            limit_price=limit,
            is_paper=is_paper,
            client_order_id=f"open-{decision.id}",
            side="SELL_TO_OPEN",
        )
        if not self.orders.insert_if_new(order):
            existing = self.orders.get_by_client_order_id(order.client_order_id)
            if existing is not None:
                if existing.status == OrderStatus.FILLED:
                    return existing
                order = existing

        self.entry_decisions.set_status(decision.id, DecisionStatus.EXECUTING)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"open": True, "occ": order.occ_symbol, "qty": order.quantity,
             "limit": order.limit_price, "is_paper": order.is_paper},
            source="executor", decision_id=decision.id, order_id=order.id,
        )

        # 6. Submit + await fill.
        try:
            order.submitted_at = order.submitted_at or utcnow()
            submitted = await self.broker.submit_open_order(order)
        except Exception as exc:  # noqa: BLE001
            log.exception("submit_open_order failed: %s", exc)
            order.status = OrderStatus.REJECTED
            self.orders.update(order)
            self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
            self.audit.record(
                AuditEventType.ERROR, {"where": "executor.open", **describe_exception(exc)},
                source="executor", decision_id=decision.id, order_id=order.id,
            )
            await self._notify("Entry FAILED", f"{decision.occ_symbol}: submit error: {exc}",
                               priority="high")
            return order

        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        final = await self._await_open_fill(submitted)

        # 7. Settle.
        if final.status == OrderStatus.FILLED:
            self.entry_decisions.set_status(decision.id, DecisionStatus.DONE)
            self.audit.record(
                AuditEventType.ORDER_FILL,
                {"open": True, "occ": final.occ_symbol, "filled_qty": final.filled_qty,
                 "avg_fill_price": final.avg_fill_price, "is_paper": final.is_paper},
                source="executor", decision_id=decision.id, order_id=final.id,
            )
            tag = "(paper)" if final.is_paper else "(LIVE)"
            await self._notify(
                f"Opened CSP {decision.underlying} {tag}",
                f"{decision.rule_name}: sold to open {final.filled_qty}x "
                f"{decision.occ_symbol} @ {final.avg_fill_price}",
                priority="high",
            )
        else:
            # Order-state confirmation is flaky on the RH MCP — a sub-second fill may not report
            # "filled" via get_order in time (verified live 2026-07-22: a filled entry was mislabeled
            # FAILED). Before declaring failure (and cancelling — which could hit a real fill), check
            # the AUTHORITATIVE position read: if the short is actually open at the broker, it filled.
            opened = False
            try:
                opened = any(p.occ_symbol == decision.occ_symbol
                             for p in await self.broker.get_open_positions())
            except Exception:  # noqa: BLE001 — fall through to the not-filled path
                pass
            if opened:
                self.entry_decisions.set_status(decision.id, DecisionStatus.DONE)
                self.audit.record(
                    AuditEventType.ORDER_FILL,
                    {"open": True, "occ": decision.occ_symbol, "confirmed_via": "position_read"},
                    source="executor", decision_id=decision.id, order_id=final.id,
                )
                await self._notify(
                    f"Opened CSP {decision.underlying} (LIVE)",
                    f"{decision.occ_symbol}: fill confirmed via position read (order-state lagged).",
                    priority="high",
                )
            else:
                try:
                    await self.broker.cancel_order(final)
                except Exception:  # noqa: BLE001
                    pass
                self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
                await self._notify(
                    "Entry did not fill",
                    f"{decision.occ_symbol}: order {final.status.value} after "
                    f"{ex.fill_timeout_seconds}s.",
                    priority="normal",
                )
        return final

    async def _await_open_fill(self, order: Order) -> Order:
        """Poll an open order to fill/terminal/timeout. No reprice (re-scan next cycle)."""
        ex = self.settings.execution
        deadline = time.monotonic() + ex.fill_timeout_seconds
        while True:
            if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return order
            if time.monotonic() >= deadline:
                return order
            await asyncio.sleep(self.poll_interval)
            try:
                order = await self.broker.get_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("get_order failed during open fill poll: %s", exc)
                continue
            order.last_status_at = utcnow()
            self.orders.update(order)

    def _open_quote_problem(self, quote: OptionContractQuote | None) -> str | None:
        if quote is None:
            return "no quote"
        if not quote.is_valid:
            return "invalid (missing/zero bid-ask)"
        if quote.is_stale(self.settings.max_quote_age_seconds):
            return f"stale ({quote.age_seconds():.0f}s > {self.settings.max_quote_age_seconds}s)"
        return None

    async def _block_entry(
        self, decision: EntryDecision, reason: str, status: DecisionStatus | None = None
    ) -> None:
        log.warning("Entry suppressed for %s: %s", decision.occ_symbol, reason)
        if status is not None and self.entry_decisions is not None:
            self.entry_decisions.set_status(decision.id, status)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "reason": reason, "occ": decision.occ_symbol,
             "rule": decision.rule_name},
            source="executor", decision_id=decision.id,
        )
        await self._notify(
            f"Entry NOT placed: {decision.underlying}", f"{decision.rule_name} — {reason}",
            priority="normal",
        )

    # ------------------------------------------------------------------ fill loop
    async def _await_fill(self, order: Order, position: Position) -> Order:
        """Poll the broker until the order fills, is terminally rejected/cancelled, or the
        fill timeout elapses. On no-fill past ``reprice_after_seconds`` it re-prices once
        (cancel + resubmit at a higher limit, still within the slippage cap)."""
        ex = self.settings.execution
        deadline = time.monotonic() + ex.fill_timeout_seconds
        repriced = False

        while True:
            if order.status == OrderStatus.FILLED:
                return order
            if order.status in (OrderStatus.CANCELLED, OrderStatus.REJECTED):
                return order
            if time.monotonic() >= deadline:
                return order

            await asyncio.sleep(self.poll_interval)
            try:
                refreshed = await self.broker.get_order(order)
            except Exception as exc:  # noqa: BLE001
                log.warning("get_order failed during fill poll: %s", exc)
                continue
            order = refreshed
            order.last_status_at = utcnow()
            self.orders.update(order)

            elapsed = ex.fill_timeout_seconds - (deadline - time.monotonic())
            if (
                not repriced
                and elapsed >= ex.reprice_after_seconds
                and order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL)
            ):
                repriced = True
                order = await self._reprice(order, position)
        # unreachable

    async def _reprice(self, order: Order, position: Position) -> Order:
        """Cancel the working order and resubmit the unfilled remainder at a fresh, higher
        limit (still slippage-capped). Returns the new/updated order to keep polling."""
        quote = await self.market_data.get_quote(position)
        if self._quote_problem(quote):
            return order  # can't safely reprice without a good quote; keep waiting
        ex = self.settings.execution
        new_limit = compute_limit_price(
            quote, buffer_pct=ex.limit_buffer_pct * 2, slippage_cap_pct=ex.slippage_cap_pct
        )
        if new_limit is None or new_limit <= order.limit_price:
            return order
        try:
            await self.broker.cancel_order(order)
        except Exception as exc:  # noqa: BLE001
            log.warning("cancel during reprice failed: %s", exc)

        remaining = max(order.quantity - order.filled_qty, 0) or order.quantity
        new_order = Order(
            decision_id=order.decision_id,
            position_id=order.position_id,
            occ_symbol=order.occ_symbol,
            option_id=order.option_id,
            quantity=remaining,
            limit_price=new_limit,
            is_paper=order.is_paper,
            client_order_id=f"{order.client_order_id}-r1",
        )
        self.orders.insert_if_new(new_order)
        self.audit.record(
            AuditEventType.ORDER_SUBMIT,
            {"reprice": True, "from": order.limit_price, "to": new_limit, "qty": remaining},
            source="executor", position_id=position.id,
            decision_id=order.decision_id, order_id=new_order.id,
        )
        new_order.submitted_at = utcnow()
        submitted = await self.broker.submit_close_order(new_order)
        submitted.last_status_at = utcnow()
        self.orders.update(submitted)
        return submitted

    # ------------------------------------------------------------------ helpers
    def _quote_problem(self, quote: OptionQuote | None) -> str | None:
        if quote is None:
            return "no quote"
        if not quote.is_valid:
            return "invalid (missing/zero bid-ask)"
        if quote.is_stale(self.settings.max_quote_age_seconds):
            return f"stale ({quote.age_seconds():.0f}s > {self.settings.max_quote_age_seconds}s)"
        return None

    async def _block(
        self,
        position: Position,
        decision: CloseDecision,
        reason: str,
        status: DecisionStatus | None = None,
    ) -> None:
        """Record + notify that an auto-close was not placed, and (optionally) set status."""
        log.warning("Close suppressed for %s: %s", position.occ_symbol, reason)
        if status is not None:
            self.decisions.set_status(decision.id, status)
        self.audit.record(
            AuditEventType.DECISION,
            {"executed": False, "reason": reason, "occ": position.occ_symbol,
             "rule": decision.rule_name},
            source="executor", position_id=position.id, decision_id=decision.id,
        )
        await self._notify(
            f"Close NOT placed: {position.underlying}",
            f"{decision.rule_name} — {reason}",
            priority="normal",
        )

    async def _notify(self, title: str, message: str, *, priority: str = "normal") -> None:
        if self.notifier is not None:
            await self.notifier.send(title, message, priority=priority)

    def _journal_outcome(self, position: Position, close_order: Order, exit_reason: str) -> None:
        """Backfill the open trade-journal row for this position with realized outcome."""
        if self.trade_journal is None:
            return
        je = self.trade_journal.find_open_by_occ(position.occ_symbol)
        if je is None:
            return
        position.status = PositionStatus.CLOSED  # so position_pnl computes the realized branch
        info = position_pnl(position, close_order)
        self.trade_journal.set_outcome(
            je.id, status=info["outcome"], realized_pnl=info["realized_pnl"],
            close_price=info["close_price"], exit_reason=exit_reason, entered_at=je.entered_at,
            mfe_pct=position.peak_profit_pct, mae_pct=position.trough_profit_pct,
        )
