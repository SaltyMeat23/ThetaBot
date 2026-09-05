"""Roll management for tested short puts on the wheel.

Instead of realizing a loss (a premium stop) or accepting assignment at an unfavorable basis, a
TESTED put near expiry is ROLLED: buy it back and sell a further-dated put (down a strike if needed)
for a NET CREDIT — lowering the effective cost basis and buying time. If no acceptable credit roll
exists, it leaves the position alone (rides to assignment — the wheel thesis). Both legs go through
the executor, so every safety gate (kill-switch, live-arming, stale-quote, persist-before-submit)
still applies.
"""
from __future__ import annotations

import logging
from datetime import date

from ..config import RollConfig
from ..domain.enums import AuditEventType, OrderStatus, RuleType
from ..domain.models import CloseDecision, EntryDecision, Position, utcnow
from ..marketdata.quote import OptionContractQuote, OptionQuote
from ..rules.base import cost_to_close, dedup_key

log = logging.getLogger("agentic.roll")


def should_roll(position: Position, quote: OptionQuote | None, cfg: RollConfig) -> str | None:
    """Reason string if this position is a roll candidate (tested + near expiry), else None."""
    if not cfg.enabled or position.credit_received <= 0:
        return None
    if position.dte() > cfg.roll_dte:
        return None
    delta = quote.delta if (quote is not None and quote.delta is not None) else position.delta
    if delta is None or abs(delta) < cfg.roll_delta:
        return None
    return f"tested (|delta| {abs(delta):.2f}) with {position.dte()}d left"


def select_roll_target(
    chain: list[OptionContractQuote], position: Position, current_cost: float,
    cfg: RollConfig, today: date | None = None,
) -> tuple[OptionContractQuote, float] | None:
    """Best further-dated put to roll into: strike <= current, in the target DTE window, not more
    tested, and a NET CREDIT >= min_net_credit. Prefers the lowest strike (best basis improvement).
    Returns (target_contract, net_credit) or None."""
    today = today or utcnow().date()
    best: tuple[OptionContractQuote, float] | None = None
    for c in chain:
        if c.option_type != "put" or not c.is_valid:
            continue
        d = c.dte(today)
        if d < cfg.target_dte_min or d > cfg.target_dte_max:
            continue
        if c.strike > position.strike:                     # roll down or flat only
            continue
        if c.delta is not None and abs(c.delta) > cfg.max_target_delta:
            continue                                        # don't roll into a more-tested put
        new_credit = c.midpoint
        if new_credit is None:
            continue
        net = round(new_credit - current_cost, 4)
        if net < cfg.min_net_credit:
            continue
        # Prefer the lowest strike (best basis improvement); tie-break on higher net credit.
        if best is None or c.strike < best[0].strike or (c.strike == best[0].strike and net > best[1]):
            best = (c, net)
    return best


class RollManager:
    def __init__(self, settings, broker, market_data, executor, decisions, entry_decisions,
                 audit, notifier=None):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.executor = executor
        self.decisions = decisions
        self.entry_decisions = entry_decisions
        self.audit = audit
        self.notifier = notifier

    async def try_roll(self, position: Position, quote: OptionQuote | None) -> bool:
        cfg = self.settings.roll
        reason = should_roll(position, quote, cfg)
        if reason is None:
            return False
        current_cost = cost_to_close(quote)
        if current_cost is None:
            return False
        try:
            chain = await self.market_data.get_chain(position.underlying)
        except Exception as exc:  # noqa: BLE001 — a bad chain fetch must not break the cycle
            log.warning("Roll chain fetch failed for %s: %s", position.underlying, exc)
            return False
        picked = select_roll_target(chain, position, current_cost, cfg)
        if picked is None:
            # A tested put with no acceptable credit roll rides to assignment. Notify ONCE per
            # position per day: this path runs every monitor cycle, so an un-deduped alert pings
            # on every poll (every few minutes, around the clock) about the same un-rollable
            # position — exactly the notification spam we don't want. A dedup marker (distinct
            # from the executed-roll key) gates it to one send per position per UTC day.
            marker = CloseDecision(
                position_id=position.id, rule_name="roll", rule_type=RuleType.ROLL,
                reason=f"No credit roll available; {reason} — leaving it to ride to assignment.",
                requires_approval=False,
                dedup_key=f"{dedup_key(position.id, RuleType.ROLL, utcnow())}:noroll",
            )
            if self.decisions.insert_if_new(marker):
                await self._notify(
                    f"No credit roll for {position.underlying} {position.occ_symbol}",
                    f"{reason}; leaving it to ride to assignment.")
            return False
        return await self._execute_roll(position, quote, picked[0], picked[1], reason)

    async def _execute_roll(self, position, quote, target, net, reason) -> bool:
        now = utcnow()
        close_dec = CloseDecision(
            position_id=position.id, rule_name="roll", rule_type=RuleType.ROLL,
            reason=f"Roll: {reason} -> {target.occ_symbol} (net +${net:.2f}).",
            requires_approval=False, dedup_key=dedup_key(position.id, RuleType.ROLL, now),
        )
        if not self.decisions.insert_if_new(close_dec):
            return False   # already rolled this position today
        close_order = await self.executor.execute_close(position, close_dec, quote)
        if close_order is None or close_order.status is not OrderStatus.FILLED:
            return False   # blocked / not filled -> do NOT open the new leg (never go naked-long)

        entry_dec = EntryDecision(
            underlying=position.underlying, occ_symbol=target.occ_symbol, option_id=target.option_id,
            strike=target.strike, expiration=target.expiration, contracts=position.quantity,
            premium=target.midpoint or 0.0, rule_name="roll",
            reason=f"Roll from {position.occ_symbol} (net +${net:.2f}).",
            dedup_key=f"roll:{target.occ_symbol}:{now.date().isoformat()}",
        )
        self.entry_decisions.insert_if_new(entry_dec)
        await self.executor.execute_open(entry_dec, target)

        self.audit.record(
            AuditEventType.DECISION,
            {"roll": True, "from": position.occ_symbol, "to": target.occ_symbol, "net_credit": net},
            source="roll", position_id=position.id,
        )
        await self._notify(
            f"Rolled {position.underlying}: {position.strike:.0f}P -> {target.strike:.0f}P",
            f"{position.occ_symbol} -> {target.occ_symbol} for net +${net:.2f} credit "
            f"({position.dte()}d -> {target.dte()}d). Effective cost basis lowered.")
        log.info("Rolled %s -> %s (net +$%.2f)", position.occ_symbol, target.occ_symbol, net)
        return True

    async def _notify(self, title, message) -> None:
        if self.notifier is not None:
            try:
                await self.notifier.send(title, message)
            except Exception:  # noqa: BLE001
                pass
