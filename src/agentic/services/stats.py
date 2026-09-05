"""Read-only performance analytics for the dashboard.

Pure functions over domain objects (positions, orders, decisions) — no DB or I/O — so they
are trivially testable. P&L covers SHORT premium only (the only thing this platform trades):
a short collects ``credit_received`` per share at open (×100 per contract); closing it pays a
debit per share. Win/loss is realized only.

Outcome rules:
  * OPEN / CLOSING  -> "open"      (unrealized P&L from current_mark if known)
  * EXPIRED         -> "win"       (short expired worthless -> keep the full credit)
  * CLOSED + fill   -> "win"/"loss" by realized P&L = (credit - close_debit) ×100 ×qty
  * CLOSED, no fill -> "win"/"loss" ESTIMATED from the last mark (``pnl_estimated=True``) when a
                       mark is known — e.g. a reconcile-detected close whose buy-to-close order
                       state lagged at the broker; only "closed" (unknown) if no mark either
  * ASSIGNED        -> "assigned"  (P&L depends on share disposition; excluded)

Fees are NOT included in v1 — broker fee fields aren't persisted yet (see robinhood_mcp
review payload's ``fees`` block for the data we'd wire in later).
"""
from __future__ import annotations

from datetime import datetime, timezone

from ..domain.enums import OrderStatus, PositionStatus
from ..domain.models import CloseDecision, Order, Position

MULTIPLIER = 100  # shares per option contract


def resolved_at(pos: Position, close_order: Order | None) -> datetime | None:
    """Best-effort timestamp a position was RESOLVED, for windowing a report to a date range.

    CLOSED -> the filled close order's submit time; EXPIRED/ASSIGNED -> ~market close on the
    expiration day. Open (or closed-without-a-fill) positions have no resolution time -> None.
    """
    if pos.status == PositionStatus.CLOSED:
        if close_order is not None:
            return close_order.submitted_at or close_order.last_status_at
        return pos.last_synced_at  # estimated close (no captured fill) -> last sync ≈ resolution
    if pos.status in (PositionStatus.EXPIRED, PositionStatus.ASSIGNED):
        e = pos.expiration
        return datetime(e.year, e.month, e.day, 20, 0, tzinfo=timezone.utc)  # ~16:00 ET
    return None


def filled_close(orders_for_pos: list[Order]) -> Order | None:
    """The filled close order for a position (latest, if a reprice created several)."""
    filled = [
        o for o in orders_for_pos
        if o.status == OrderStatus.FILLED and o.avg_fill_price is not None
    ]
    if not filled:
        return None
    return sorted(filled, key=lambda o: (o.submitted_at is not None, o.submitted_at, o.id))[-1]


def position_pnl(pos: Position, close_order: Order | None) -> dict:
    """Classify a position and compute realized/unrealized P&L in dollars."""
    gross_credit = pos.credit_received * MULTIPLIER * pos.quantity
    out: dict = {
        "outcome": "open",
        "realized_pnl": None,
        "unrealized_pnl": None,
        "close_price": None,
        "credit": gross_credit,
        "pnl_estimated": False,
    }
    if pos.status in (PositionStatus.OPEN, PositionStatus.CLOSING):
        if pos.current_mark is not None:
            out["unrealized_pnl"] = round(
                (pos.credit_received - pos.current_mark) * MULTIPLIER * pos.quantity, 2
            )
        return out
    if pos.status == PositionStatus.EXPIRED:
        out["outcome"] = "win"
        out["realized_pnl"] = round(gross_credit, 2)
        return out
    if pos.status == PositionStatus.ASSIGNED:
        out["outcome"] = "assigned"
        return out
    if pos.status == PositionStatus.CLOSED:
        debit = close_order.avg_fill_price if close_order is not None else None
        if debit is None and pos.current_mark is not None:
            # No captured fill (e.g. a reconcile-detected close whose buy-to-close order state
            # lagged at the broker). Estimate the close debit from the last known mark so the trade
            # still counts toward P&L instead of silently vanishing — flagged as an estimate.
            debit = pos.current_mark
            out["pnl_estimated"] = True
        if debit is not None:
            realized = (pos.credit_received - debit) * MULTIPLIER * pos.quantity
            out["close_price"] = round(debit, 4)
            out["realized_pnl"] = round(realized, 2)
            out["outcome"] = "win" if realized > 0 else "loss"
        else:
            out["outcome"] = "closed"  # no fill and no mark -> genuinely unknown
    return out


def _orders_by_position(orders: list[Order]) -> dict[str, list[Order]]:
    by_pos: dict[str, list[Order]] = {}
    for o in orders:
        by_pos.setdefault(o.position_id, []).append(o)
    return by_pos


def _rule_by_position(decisions: list[CloseDecision]) -> dict[str, str]:
    """Latest decision's rule_name per position (for per-rule attribution)."""
    rule_by_pos: dict[str, str] = {}
    for d in sorted(decisions, key=lambda d: d.created_at):
        rule_by_pos[d.position_id] = d.rule_name
    return rule_by_pos


def _is_paper_position(p: Position, paper_occs: set[str]) -> bool:
    """Whether a position is paper (for real-only filtering).

    Prefer the authoritative flag stamped on the position at sync from the active broker. Fall
    back to the OCC of any paper order for positions predating that flag (``is_paper is None``) —
    a scanner-opened position's fill order carries ``position_id=""`` (the position is created
    later by reconcile), so it can't be matched by id; the OCC catches it.
    """
    if p.is_paper is not None:
        return p.is_paper
    return p.occ_symbol in paper_occs


def position_rows(
    positions: list[Position], orders: list[Order], decisions: list[CloseDecision],
    real_only: bool = False,
) -> list[dict]:
    """Per-position view rows (P&L + attributed rule) for /api/positions.

    ``real_only`` drops paper positions — used in live mode so views/reports show only real trades,
    not leftover paper-soak history.
    """
    by_pos = _orders_by_position(orders)
    rule_by_pos = _rule_by_position(decisions)
    paper_occs = {o.occ_symbol for o in orders if o.is_paper}
    rows: list[dict] = []
    for p in positions:
        pos_orders = by_pos.get(p.id, [])
        if real_only and _is_paper_position(p, paper_occs):
            continue  # paper position — excluded from live views
        info = position_pnl(p, filled_close(pos_orders))
        rows.append({
            "occ_symbol": p.occ_symbol,
            "underlying": p.underlying,
            "strategy": p.strategy.value,
            "option_type": p.option_type.value,
            "status": p.status.value,
            "quantity": p.quantity,
            "strike": p.strike,
            "expiration": p.expiration.isoformat(),
            "dte": p.dte(),
            "credit_received": p.credit_received,
            "current_mark": p.current_mark,
            "outcome": info["outcome"],
            "realized_pnl": info["realized_pnl"],
            "unrealized_pnl": info["unrealized_pnl"],
            "close_price": info["close_price"],
            "pnl_estimated": info["pnl_estimated"],
            "rule": rule_by_pos.get(p.id),
        })
    return rows


def compute_stats(
    positions: list[Position], orders: list[Order], decisions: list[CloseDecision],
    since: datetime | None = None, real_only: bool = False,
) -> dict:
    """Aggregate win rate, realized/unrealized P&L, and per-rule rollups.

    ``since`` windows the RESOLVED trades (wins/losses/assigned/credit/by_rule) to those resolved
    at/after that time — for a true trailing-week report. Open positions and unrealized P&L always
    reflect the CURRENT book (they are live state, not a windowed event). ``real_only`` drops paper
    positions (live mode) so reports reflect only real trades, not leftover paper-soak history.
    """
    by_pos = _orders_by_position(orders)
    rule_by_pos = _rule_by_position(decisions)
    paper_occs = {o.occ_symbol for o in orders if o.is_paper}

    wins = losses = assigned = open_count = 0
    realized = unrealized = credit_resolved = 0.0
    by_rule: dict[str, dict] = {}
    by_status: dict[str, int] = {}

    for p in positions:
        pos_orders = by_pos.get(p.id, [])
        if real_only and _is_paper_position(p, paper_occs):
            continue  # paper position — excluded from live reports
        by_status[p.status.value] = by_status.get(p.status.value, 0) + 1
        close_order = filled_close(pos_orders)
        info = position_pnl(p, close_order)
        outcome = info["outcome"]
        if outcome == "open":
            open_count += 1
            if info["unrealized_pnl"] is not None:
                unrealized += info["unrealized_pnl"]
            continue
        if since is not None:
            rat = resolved_at(p, close_order)
            if rat is None or rat < since:
                continue  # resolved outside the window -> not part of this week
        if outcome in ("assigned", "closed"):
            assigned += outcome == "assigned"
            continue
        # win or loss -> realized
        realized += info["realized_pnl"]
        credit_resolved += info["credit"]
        rname = rule_by_pos.get(p.id, "expiry/other")
        bucket = by_rule.setdefault(
            rname, {"rule": rname, "closes": 0, "wins": 0, "realized_pnl": 0.0}
        )
        bucket["closes"] += 1
        bucket["realized_pnl"] = round(bucket["realized_pnl"] + info["realized_pnl"], 2)
        if outcome == "win":
            wins += 1
            bucket["wins"] += 1
        else:
            losses += 1

    resolved = wins + losses
    return {
        "open_count": open_count,
        "resolved_count": resolved,
        "wins": wins,
        "losses": losses,
        "assigned": assigned,
        "win_rate": (wins / resolved) if resolved else None,
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(unrealized, 2),
        "credit_collected_resolved": round(credit_resolved, 2),
        "by_rule": sorted(by_rule.values(), key=lambda r: r["realized_pnl"], reverse=True),
        "by_status": by_status,
    }
