"""compute_stats(since=...) windows RESOLVED trades to a trailing period (true weekly report)."""
from datetime import date, datetime, timedelta, timezone

from agentic.domain.enums import OptionType, OrderStatus, PositionStatus, Strategy
from agentic.domain.models import Order, Position
from agentic.services.stats import compute_stats, resolved_at

NOW = datetime.now(timezone.utc)


def _pos(occ, status, credit, mark=None, exp_days=10):
    return Position(
        occ_symbol=occ, underlying="F", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, quantity=1, strike=12.0,
        expiration=date.today() + timedelta(days=exp_days), credit_received=credit,
        current_mark=mark, status=status,
    )


def _closed(occ, credit, debit, submitted_at):
    p = _pos(occ, PositionStatus.CLOSED, credit)
    o = Order(decision_id="d", position_id=p.id, occ_symbol=occ, quantity=1, limit_price=debit,
              is_paper=True, status=OrderStatus.FILLED, avg_fill_price=debit,
              submitted_at=submitted_at)
    return p, o


def test_window_excludes_old_resolutions():
    p_recent, o_recent = _closed("RECENT", 1.0, 0.5, NOW - timedelta(days=2))   # +50, this week
    p_old, o_old = _closed("OLD", 1.0, 0.5, NOW - timedelta(days=30))           # +50, last month
    positions, orders = [p_recent, p_old], [o_recent, o_old]
    cutoff = NOW - timedelta(days=7)

    windowed = compute_stats(positions, orders, [], since=cutoff)
    assert windowed["wins"] == 1 and windowed["realized_pnl"] == 50.0           # recent only

    allt = compute_stats(positions, orders, [])
    assert allt["wins"] == 2 and allt["realized_pnl"] == 100.0                  # both, no window


def test_open_positions_stay_current_regardless_of_window():
    p_open = _pos("OPEN", PositionStatus.OPEN, 1.5, mark=1.0)                    # unrealized +50
    s = compute_stats([p_open], [], [], since=NOW - timedelta(days=7))
    assert s["open_count"] == 1 and s["unrealized_pnl"] == 50.0                 # window never hides open


def test_resolved_at_expired_uses_expiration():
    rat = resolved_at(_pos("EXP", PositionStatus.EXPIRED, 1.0, exp_days=-1), None)
    assert rat is not None and rat.tzinfo is not None
    assert resolved_at(_pos("OPEN", PositionStatus.OPEN, 1.0), None) is None     # open -> not resolved


def test_real_only_excludes_paper_positions():
    p_paper, o_paper = _closed("PAPER", 1.0, 0.5, NOW - timedelta(days=1))   # +50, paper order
    p_real, o_real = _closed("REAL", 1.0, 0.5, NOW - timedelta(days=1))      # +50, real order
    o_paper.is_paper = True
    o_real.is_paper = False
    positions, orders = [p_paper, p_real], [o_paper, o_real]

    assert compute_stats(positions, orders, [])["wins"] == 2                 # both by default
    live = compute_stats(positions, orders, [], real_only=True)
    assert live["wins"] == 1 and live["realized_pnl"] == 50.0                # real only
    from agentic.services.stats import position_rows
    rows = position_rows(positions, orders, [], real_only=True)
    assert [r["occ_symbol"] for r in rows] == ["REAL"]

# --- real_only filtering: paper trades must not count in live views (the T $21.5P bug) ---

def test_real_only_hides_paper_by_stamped_flag():
    """A position stamped is_paper=True is excluded from live views/stats."""
    from agentic.services.stats import position_rows
    real = _pos("REAL", PositionStatus.OPEN, 0.20, mark=0.10); real.is_paper = False
    paper = _pos("PAPR", PositionStatus.OPEN, 0.20, mark=0.10); paper.is_paper = True

    rows = position_rows([real, paper], [], [], real_only=True)
    assert [r["occ_symbol"] for r in rows] == ["REAL"]
    s = compute_stats([real, paper], [], [], real_only=True)
    assert s["open_count"] == 1

    # Without real_only, both show.
    assert len(position_rows([real, paper], [], [])) == 2


def test_real_only_hides_historical_paper_via_occ_fallback():
    """A resolved paper entry whose fill order has position_id='' (scanner entries) and whose
    position predates the is_paper flag (is_paper=None) is still caught by its paper order's OCC —
    the exact T260724P00021500 case that was leaking a +$24 'win' into live stats."""
    t = _pos("T260724P00021500", PositionStatus.EXPIRED, 0.24)  # is_paper defaults None
    # Its opening order: paper, and position_id="" (as execute_open creates entry orders).
    open_order = Order(decision_id="d", position_id="", occ_symbol="T260724P00021500",
                       quantity=1, limit_price=0.24, is_paper=True, side="SELL_TO_OPEN",
                       status=OrderStatus.FILLED, avg_fill_price=0.24)

    live = compute_stats([t], [open_order], [], real_only=True)
    assert live["wins"] == 0 and live["realized_pnl"] == 0.0     # excluded from live
    allt = compute_stats([t], [open_order], [])
    assert allt["wins"] == 1 and allt["realized_pnl"] == 24.0    # still visible without real_only


# --- estimated close: a reconcile-detected buyback with no captured fill order ------------------

def test_closed_without_fill_estimates_pnl_from_mark():
    """A CLOSED short put with no filled close order (RH order-state lag) is scored from its last
    mark instead of vanishing — this is the ONDS 7.5P case that read realized_pnl=null."""
    from agentic.services.stats import position_pnl
    # credit 0.20, bought back ~0.58 -> ~-$38 loss, flagged estimated.
    p = _pos("ONDS260731P00007500", PositionStatus.CLOSED, 0.20, mark=0.58)
    info = position_pnl(p, None)
    assert info["outcome"] == "loss"
    assert info["realized_pnl"] == -38.0
    assert info["close_price"] == 0.58
    assert info["pnl_estimated"] is True


def test_closed_without_fill_or_mark_stays_unknown():
    from agentic.services.stats import position_pnl
    p = _pos("X", PositionStatus.CLOSED, 0.20, mark=None)   # no fill, no mark
    info = position_pnl(p, None)
    assert info["outcome"] == "closed" and info["realized_pnl"] is None


def test_estimated_close_counts_in_stats():
    """The estimated loss lands in compute_stats (win rate + realized P&L), not dropped."""
    p = _pos("ONDS260731P00007500", PositionStatus.CLOSED, 0.20, mark=0.58)
    p.last_synced_at = NOW
    s = compute_stats([p], [], [])
    assert s["losses"] == 1 and s["resolved_count"] == 1
    assert s["realized_pnl"] == -38.0


def test_real_fill_beats_mark_estimate():
    """When a filled close order exists, its price wins and pnl_estimated stays False."""
    from agentic.services.stats import filled_close, position_pnl
    p, o = _closed("F260807P00012000", 1.0, 0.40, NOW)   # authoritative debit 0.40 -> +$60
    p.current_mark = 0.55                                 # a misleading later mark must be ignored
    info = position_pnl(p, filled_close([o]))
    assert info["realized_pnl"] == 60.0 and info["pnl_estimated"] is False
