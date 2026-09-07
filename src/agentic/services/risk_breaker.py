"""Portfolio loss circuit breaker for a premium-SELLING book.

Freezes NEW option-selling entries when REALIZED losses pile up. It NEVER force-closes: the monitor
keeps managing/closing existing positions, so short puts are never dumped at the bottom (that would
lock in max loss exactly when the wheel says to hold through / take assignment). It keys on realized
P&L and losing streaks — not unrealized marks — because a short put's mark swing is noise for a
hold-to-expiry seller, and assignment is the strategy working, not a loss.

Pure function: the scanner calls it each cycle with the current account value and gates new entries
on ``tripped``. Auto-evaluates, so it clears itself as losing trades roll out of the window.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


def evaluate_risk_breaker(journal, cfg, account_value: float | None, now: datetime) -> dict[str, Any]:
    """Return the breaker state. ``tripped`` True means: freeze NEW entries this cycle (do not close
    anything). ``journal`` needs ``realized_since(since_iso)`` and ``resolved_pnls(limit)``."""
    state: dict[str, Any] = {
        "enabled": bool(cfg.loss_breaker_enabled),
        "tripped": False,
        "reason": None,
        "window_days": cfg.lookback_days,
        "window_realized": 0.0,
        "loss_limit": None,
        "consecutive_losses": 0,
    }
    if not cfg.loss_breaker_enabled or journal is None:
        return state

    # 1) Rolling-window realized loss vs % of account.
    since = (now - timedelta(days=cfg.lookback_days)).isoformat()
    window_realized, _n = journal.realized_since(since)
    state["window_realized"] = window_realized
    if cfg.max_realized_loss_pct and account_value and account_value > 0:
        limit = -abs(cfg.max_realized_loss_pct) * account_value
        state["loss_limit"] = round(limit, 2)
        if window_realized <= limit:
            state["tripped"] = True
            state["reason"] = (
                f"realized {window_realized:+.0f} over {cfg.lookback_days}d "
                f"<= {limit:.0f} ({cfg.max_realized_loss_pct:.0%} of account)"
            )

    # 2) Consecutive realized losers (newest-first; count the leading run of negatives).
    if cfg.max_consecutive_losses and cfg.max_consecutive_losses > 0:
        streak = 0
        for pnl in journal.resolved_pnls(limit=cfg.max_consecutive_losses + 5):
            if pnl is not None and pnl < 0:
                streak += 1
            else:
                break
        state["consecutive_losses"] = streak
        if streak >= cfg.max_consecutive_losses and not state["tripped"]:
            state["tripped"] = True
            state["reason"] = (
                f"{streak} consecutive realized losses (limit {cfg.max_consecutive_losses})"
            )

    return state


def apply_sector_cap(approved, open_positions, cfg, account_value):
    """Drop approved CSP entries that would push a sector over ``max_pct_per_sector`` of account value.

    Prevents a book of many names from concentrating into one correlated theme (the failure mode
    where 20 'diversified' positions are really one AI/nuclear/crypto bet). Entries are considered in
    ranked order (``approved`` is already ranked best-first), so the richest opportunities in a sector
    fill first and the marginal ones are dropped once the sector is full. Sector exposure counts
    existing short-put collateral. Fail-open: no cap / no account value -> keep everything.

    Returns ``(kept, skipped)`` where ``skipped`` is a list of ``(candidate, reason)`` mirroring the
    sizer's rejected shape, so the scanner can log them as negative examples.
    """
    cap = cfg.max_pct_per_sector
    if not cap or not account_value or account_value <= 0:
        return list(approved), []
    smap = cfg.sector_map or {}

    def sector(sym: str) -> str:
        return smap.get(sym.upper()) or smap.get(sym) or sym.upper()

    exposure: dict[str, float] = {}
    for p in open_positions:
        ot = getattr(p, "option_type", None)
        if str(getattr(ot, "value", ot)).lower() == "put":
            coll = (p.strike or 0) * 100 * (p.quantity or 0)
            exposure[sector(p.underlying)] = exposure.get(sector(p.underlying), 0.0) + coll

    limit = cap * account_value
    kept, skipped = [], []
    for e in approved:
        sec = sector(e.candidate.underlying)
        if exposure.get(sec, 0.0) + e.collateral > limit:
            skipped.append((e.candidate,
                            f"sector cap: '{sec}' would exceed {cap:.0%} of account"))
        else:
            exposure[sec] = exposure.get(sec, 0.0) + e.collateral
            kept.append(e)
    return kept, skipped
