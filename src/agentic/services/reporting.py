"""Scheduled push reports for hands-off operation.

For a user who can't watch during market hours, these are the primary way the bot proves it's alive
and shows what it did: a DAILY DIGEST (heartbeat — so silence means "all fine", not "it died") and a
WEEKLY performance rollup. Pure builders (testable) + a thin loop that fires each once per period.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta, timezone

from .stats import compute_stats, position_rows

log = logging.getLogger("agentic.reporting")


def now_et(now: datetime | None = None) -> datetime:
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        return now.astimezone(ZoneInfo("America/New_York"))
    except Exception:  # noqa: BLE001 — missing tzdata; fall back to UTC
        return now


def should_send_daily(cur: datetime, last_sent: date | None, hour: int, minute: int) -> bool:
    """Once per calendar day, after the configured local time."""
    if last_sent == cur.date():
        return False
    return cur.time() >= time(hour, minute)


def should_send_weekly(
    cur: datetime, last_sent: date | None, weekday: int, hour: int, minute: int
) -> bool:
    """On the configured weekday, after the configured local time, once."""
    if cur.weekday() != weekday or cur.time() < time(hour, minute):
        return False
    return last_sent != cur.date()


def _money(x: float | None) -> str:
    return "$0" if x is None else f"${x:,.0f}"


def build_daily_digest(*, stats: dict, rows: list[dict], scanner, mode: str,
                       tv_health: dict | None = None, degraded: bool = False) -> tuple[str, str]:
    open_rows = [r for r in rows if r["status"] in ("OPEN", "CLOSING")]
    scanned = getattr(scanner, "last_scan_at", None) if scanner else None
    err = getattr(scanner, "last_error", None) if scanner else None
    n_cand = len(getattr(scanner, "last_candidates", []) or []) if scanner else 0
    n_skip = len(getattr(scanner, "last_skips", []) or []) if scanner else 0

    lines = []
    if degraded:
        lines.append("!! ON SIMULATOR — live mode but the broker fell back to PAPER. NOT trading "
                     "your real account. Fix the RH connection.")
    lines += [
        f"Mode {mode} · {len(open_rows)} open · realized {_money(stats.get('realized_pnl'))} "
        f"· unrealized {_money(stats.get('unrealized_pnl'))}",
        f"Resolved {stats.get('resolved_count', 0)} "
        f"(W {stats.get('wins', 0)}/L {stats.get('losses', 0)}"
        + (f", {stats['win_rate']*100:.0f}% win" if stats.get('win_rate') is not None else "")
        + ")",
        f"Last scan {scanned.strftime('%m-%d %H:%M') if scanned else 'none'} "
        f"· {n_cand} candidates · {n_skip} skipped",
    ]
    for r in open_rows[:8]:
        pnl = r.get("unrealized_pnl")
        lines.append(
            f"  {r['underlying']} {r['strike']:g}{r['option_type'][:1].upper()} "
            f"x{r['quantity']} · {r['dte']}d · {_money(pnl)}"
        )
    if tv_health is not None:
        from .tv_health import tv_health_digest_line
        tv_line = tv_health_digest_line(tv_health)
        if tv_line:
            lines.append(tv_line)
    if err:
        lines.append(f"! last error: {str(err)[:120]}")
    title = f"Bot daily · {len(open_rows)} open · {_money(stats.get('realized_pnl'))} realized"
    return title, "\n".join(lines)


def build_weekly_report(*, stats: dict, rows: list[dict], cumulative: dict | None = None,
                        ai_summary: str | None = None, days: int = 7) -> tuple[str, str]:
    """Weekly rollup. ``stats`` is the trailing-window rollup (realized/W-L/by-rule scoped to the
    last ``days``); ``cumulative`` (optional) adds a since-inception context line; ``ai_summary``
    (optional) appends a short AI-written narrative. Open/unrealized reflect the current book.
    """
    open_rows = [r for r in rows if r["status"] in ("OPEN", "CLOSING")]
    wr = stats.get("win_rate")
    lines = [
        f"This week (last {days}d):",
        f"Realized P&L {_money(stats.get('realized_pnl'))} · "
        f"credit collected {_money(stats.get('credit_collected_resolved'))}",
        f"Resolved {stats.get('resolved_count', 0)} · W {stats.get('wins', 0)} / L "
        f"{stats.get('losses', 0)} / assigned {stats.get('assigned', 0)}"
        + (f" · {wr*100:.0f}% win" if wr is not None else " · win rate n/a"),
    ]
    for b in stats.get("by_rule", [])[:6]:
        lines.append(f"  {b['rule']}: {b['closes']}x, {b['wins']}W, {_money(b['realized_pnl'])}")
    lines.append(f"Open now: {len(open_rows)} · unrealized {_money(stats.get('unrealized_pnl'))}")
    if cumulative is not None:
        cwr = cumulative.get("win_rate")
        lines.append(
            f"Since inception: {_money(cumulative.get('realized_pnl'))} realized · "
            f"{cumulative.get('resolved_count', 0)} trades"
            + (f" · {cwr*100:.0f}% win" if cwr is not None else ""))
    if ai_summary:
        lines.append("")
        lines.append(ai_summary.strip())
    return "Bot weekly report", "\n".join(lines)


async def _weekly_ai_summary(settings, week_stats, cumulative, rows) -> str | None:
    """Best-effort AI narrative for the weekly push. Fail-open: None -> report sends without it."""
    if not getattr(settings, "ai", None) or not settings.ai.enabled:
        return None
    try:
        from ..ai.client import build_reviewer_client
        from ..ai.weekly import generate_weekly_summary
        client = build_reviewer_client(settings.ai)
        if client is None:
            return None
        return await generate_weekly_summary(
            client, week_stats=week_stats, cumulative=cumulative, rows=rows)
    except Exception as exc:  # noqa: BLE001 — a bad summary must never block the report
        log.warning("Weekly AI summary unavailable: %s", exc)
        return None


async def render_weekly(settings, positions, orders, decisions, *, days: int = 7) -> tuple[str, str]:
    """Build the weekly report (trailing window + cumulative context + AI narrative). Shared by the
    scheduled loop and the /control/preview-weekly endpoint so both render identically."""
    pos = positions.list_all()
    ords = orders.list_all()
    decs = decisions.recent(1000)
    real = settings.is_live  # live: report only real trades, not paper-soak history
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    week_stats = compute_stats(pos, ords, decs, since=cutoff, real_only=real)
    cumulative = compute_stats(pos, ords, decs, real_only=real)
    rows = position_rows(pos, ords, decs, real_only=real)
    ai_summary = await _weekly_ai_summary(settings, week_stats, cumulative, rows)
    return build_weekly_report(
        stats=week_stats, rows=rows, cumulative=cumulative, ai_summary=ai_summary, days=days)


class ReportingLoop:
    def __init__(self, settings, positions, orders, decisions, notifier,
                 scanner=None, trade_journal=None, tv_indicators=None):
        self.settings = settings
        self.positions = positions
        self.orders = orders
        self.decisions = decisions
        self.notifier = notifier
        self.scanner = scanner
        self.trade_journal = trade_journal
        self.tv_indicators = tv_indicators
        self._stop = asyncio.Event()
        self._last_daily: date | None = None
        self._last_weekly: date | None = None

    def stop(self) -> None:
        self._stop.set()

    def _gather(self):
        pos = self.positions.list_all()
        orders = self.orders.list_all()
        decisions = self.decisions.recent(1000)
        real = self.settings.is_live  # live: only real trades in the daily digest
        return (compute_stats(pos, orders, decisions, real_only=real),
                position_rows(pos, orders, decisions, real_only=real))

    async def run(self) -> None:
        cfg = self.settings.reporting
        if not cfg.enabled:
            log.info("Reporting disabled (reporting.enabled=false).")
            return
        log.info("Reporting loop started (daily %02d:%02d ET, weekly weekday=%d).",
                 cfg.daily_digest_hour, cfg.daily_digest_minute, cfg.weekly_report_weekday)
        while not self._stop.is_set():
            try:
                cur = now_et()
                if should_send_daily(cur, self._last_daily,
                                     cfg.daily_digest_hour, cfg.daily_digest_minute):
                    stats, rows = self._gather()
                    tv_health = None
                    if self.tv_indicators is not None:
                        from .tv_health import build_tv_health
                        tv_health = build_tv_health(
                            self.tv_indicators, self.settings.entry.watchlist,
                            self.settings.ai.tv_indicator_max_age_seconds)
                    degraded = False
                    brk = getattr(self.scanner, "broker", None) if self.scanner else None
                    if brk is not None:
                        from ..brokers.factory import broker_degraded
                        degraded = broker_degraded(self.settings, brk)
                    title, msg = build_daily_digest(
                        stats=stats, rows=rows, scanner=self.scanner, mode=self.settings.mode,
                        tv_health=tv_health, degraded=degraded)
                    await self.notifier.send(title, msg)
                    self._last_daily = cur.date()
                    log.info("Sent daily digest.")
                if should_send_weekly(cur, self._last_weekly, cfg.weekly_report_weekday,
                                      cfg.daily_digest_hour, cfg.daily_digest_minute):
                    title, msg = await render_weekly(
                        self.settings, self.positions, self.orders, self.decisions)
                    await self.notifier.send(title, msg)
                    self._last_weekly = cur.date()
                    log.info("Sent weekly report.")
            except Exception as exc:  # noqa: BLE001 — a bad report must not kill the loop
                log.exception("Reporting cycle error: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=cfg.check_interval_seconds)
            except asyncio.TimeoutError:
                pass
