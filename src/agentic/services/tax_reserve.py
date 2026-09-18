"""Tax-reserve sweep: once a week, move a fixed share of the NET realized gains into a cash-equivalent
ETF (SGOV by default) so the tax bill is funded as the account grows.

Schedule: the configured ET weekday/hour/minute (default Friday 15:40, inside regular hours because
share market orders only fill then). Each period:

    net = realized P&L closed since the previous period end + any loss carried forward
    net <= 0                     -> skipped, carry the (negative) balance forward
    pct * net < min_order        -> skipped, carry the gain forward
    dry_run / not live / paper   -> dry_run row (paper broker still fills a simulated order)
    else                         -> buy $pct*net of the reserve ETF, market order, regular hours

One ledger row per period (UNIQUE on period_end) so restarts never double-buy; a missed Friday is
caught up at the next regular-hours poll. The bot never sells the reserve.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from typing import Any

from ..domain.enums import AuditEventType
from ..domain.models import utcnow
from .market_hours import is_market_hours
from .reporting import now_et

log = logging.getLogger("agentic.tax_reserve")
_NS = uuid.UUID("7c9f2f8e-2c3e-4c5a-9b1e-4d2a6f8c9e10")


def scheduled_period_end(cur_et: datetime, weekday: int, hour: int, minute: int) -> datetime | None:
    """The most recent scheduled sweep instant (ET) at or before ``cur_et``; None if the very first
    scheduled instant of this week hasn't arrived yet AND there was no earlier one (never true in
    practice, but keeps the function total)."""
    days_back = (cur_et.weekday() - weekday) % 7
    cand = (cur_et - timedelta(days=days_back)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand > cur_et:
        cand -= timedelta(days=7)
    return cand


def period_key(dt_et: datetime) -> str:
    """Ledger key: the sweep instant in UTC ISO (matches the journal's closed_at clock)."""
    return dt_et.astimezone(timezone.utc).isoformat()


class TaxReserveLoop:
    def __init__(self, settings, broker, market_data, trade_journal, store, audit, killswitch,
                 notifier=None):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.journal = trade_journal
        self.store = store
        self.audit = audit
        self.killswitch = killswitch
        self.notifier = notifier
        self._stop = asyncio.Event()
        self.last_result: dict[str, Any] | None = None
        self._waiting_logged: str | None = None

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ status (for the API)
    def status(self, now: datetime | None = None) -> dict[str, Any]:
        cfg = self.settings.tax_reserve
        cur = now_et(now)
        last = self.store.last() if self.store is not None else None
        since = last["period_end"] if last else "1970-01-01T00:00:00+00:00"
        pending, n = self.journal.realized_since(since) if self.journal is not None else (0.0, 0)
        carry_in = min(0.0, last["carry_out"]) if last else 0.0
        sched = scheduled_period_end(cur, cfg.weekday, cfg.hour, cfg.minute)
        nxt = sched + timedelta(days=7) if (sched and self.store is not None and self.store.has_period(period_key(sched))) else sched
        return {"enabled": cfg.enabled, "dry_run": cfg.dry_run, "pct": cfg.pct, "symbol": cfg.symbol,
                "next_sweep_at": nxt.isoformat() if nxt else None,
                "pending_net_since_last": round(pending + carry_in, 2), "pending_closes": n,
                "carry_in": carry_in, "would_sweep": round(max(0.0, cfg.pct * (pending + carry_in)), 2),
                "last_result": self.last_result}

    # ------------------------------------------------------------------ one cycle
    async def run_once(self, now: datetime | None = None) -> dict[str, Any] | None:
        cfg = self.settings.tax_reserve
        if not cfg.enabled or self.store is None or self.journal is None:
            return None
        cur = now_et(now)
        sched = scheduled_period_end(cur, cfg.weekday, cfg.hour, cfg.minute)
        if sched is None:
            return None
        key = period_key(sched)
        if self.store.has_period(key):
            return None
        if not is_market_hours(now):
            if self._waiting_logged != key:
                log.info("Tax reserve sweep for %s is due; waiting for regular hours.", key)
                self._waiting_logged = key
            return None
        if self.killswitch is not None and self.killswitch.is_paused():
            log.info("Tax reserve sweep for %s deferred: kill switch paused.", key)
            return None

        last = self.store.last()
        period_start = last["period_end"] if last else "1970-01-01T00:00:00+00:00"
        carry_in = min(0.0, last["carry_out"]) if last else 0.0
        realized, n_closes = self.journal.realized_since(period_start)
        # only closes strictly before this period's end count (a later scan may add more)
        net = round(realized + carry_in, 2)
        amount = round(cfg.pct * net, 2) if net > 0 else 0.0
        base = dict(period_start=period_start, period_end=key, net_realized=realized, carry_in=carry_in,
                    symbol=cfg.symbol, meta={"closes": n_closes, "pct": cfg.pct})
        result: dict[str, Any]
        if net <= 0:
            self.store.record(**base, carry_out=net, amount_due=0.0, status="skipped",
                              error="net realized <= 0; carried forward")
            result = {"status": "skipped", "net": net, "amount": 0.0, "carry_out": net}
        elif amount < cfg.min_order_dollars:
            self.store.record(**base, carry_out=net, amount_due=amount, status="skipped",
                              error=f"amount {amount:.2f} below min order; carried forward")
            result = {"status": "skipped", "net": net, "amount": amount, "carry_out": net}
        else:
            caps = self.broker.capabilities()
            real_money = self.settings.is_live and not caps.is_paper
            if real_money and (cfg.dry_run or not getattr(caps, "supports_equity_orders", False)):
                why = "dry_run" if cfg.dry_run else "broker has no equity order tool"
                self.store.record(**base, carry_out=0.0, amount_due=amount, status="dry_run",
                                  dollar_amount=amount, error=why)
                result = {"status": "dry_run", "net": net, "amount": amount, "why": why}
                await self._notify(f"Tax reserve (dry run): would buy ${amount:,.2f} of {cfg.symbol}",
                                   f"Week net realized {net:+,.2f} x {cfg.pct:.0%}. No order placed ({why}).")
            else:
                result = await self._buy(base, net, amount, key)
        self.last_result = result
        self.audit.record(AuditEventType.DECISION, {"tax_reserve": True, **result}, source="tax_reserve")
        return result

    async def _buy(self, base: dict, net: float, amount: float, key: str) -> dict[str, Any]:
        cfg = self.settings.tax_reserve
        price = None
        try:
            price = await self.market_data.get_underlying_price(cfg.symbol)
        except Exception as exc:  # noqa: BLE001
            log.warning("reserve price read failed for %s: %s", cfg.symbol, exc)
        ref_id = str(uuid.uuid5(_NS, key))
        try:
            filled = await self.broker.submit_equity_order(
                symbol=cfg.symbol, side="buy", dollar_amount=amount, order_type="market",
                ref_id=ref_id, price_hint=price)
        except Exception as exc:  # noqa: BLE001
            self.store.record(**base, carry_out=net, amount_due=amount, status="failed",
                              dollar_amount=amount, ref_id=ref_id, error=str(exc)[:300])
            self.audit.record(AuditEventType.ERROR, {"where": "tax_reserve.buy", "error": str(exc)[:300],
                                                     "amount": amount}, source="tax_reserve")
            await self._notify(f"Tax reserve sweep FAILED ({cfg.symbol})",
                               f"Tried to buy ${amount:,.2f}: {exc}. Balance carried forward.", priority="high")
            return {"status": "failed", "net": net, "amount": amount, "error": str(exc)[:200]}
        status = str(filled.get("status") or "").lower()
        ok = status in ("filled", "partial")
        self.store.record(**base, carry_out=(0.0 if ok else net), amount_due=amount,
                          status=("filled" if ok else "failed"), dollar_amount=filled.get("dollars", amount),
                          shares=filled.get("shares"), fill_price=filled.get("avg_price"),
                          broker_order_id=filled.get("order_id"), ref_id=ref_id,
                          error=(None if ok else f"order state {status or 'unknown'}"))
        if ok:
            await self._notify(f"Tax reserve: bought ${filled.get('dollars', amount):,.2f} of {cfg.symbol}",
                               f"Week net realized {net:+,.2f} x {cfg.pct:.0%} -> {filled.get('shares')} sh "
                               f"@ {filled.get('avg_price')}. Reserve is walled off from trading.")
        else:
            await self._notify(f"Tax reserve order not filled ({cfg.symbol})",
                               f"State {status or 'unknown'} for ${amount:,.2f}; carried forward.", priority="high")
        return {"status": "filled" if ok else "failed", "net": net, "amount": amount,
                "shares": filled.get("shares"), "avg_price": filled.get("avg_price"),
                "order_id": filled.get("order_id")}

    async def _notify(self, title: str, message: str, *, priority: str = "normal") -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier.send(title, message, priority=priority)
        except Exception as exc:  # noqa: BLE001
            log.warning("tax reserve notify failed: %s", exc)

    async def run(self) -> None:
        cfg = self.settings.tax_reserve
        log.info("Tax reserve loop started (enabled=%s, %.0f%% of net gains -> %s, weekday=%d %02d:%02d ET, dry_run=%s).",
                 cfg.enabled, cfg.pct * 100, cfg.symbol, cfg.weekday, cfg.hour, cfg.minute, cfg.dry_run)
        while not self._stop.is_set():
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 -- a bad cycle must not kill the loop
                log.exception("Tax reserve cycle error: %s", exc)
                self.audit.record(AuditEventType.ERROR, {"where": "tax_reserve.run", "error": str(exc)[:300]},
                                  source="tax_reserve")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=cfg.check_interval_seconds)
            except asyncio.TimeoutError:
                pass
