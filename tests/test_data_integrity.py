"""Data-integrity fixes: multi-episode position store + paper-broker persistence across restart."""
from datetime import timedelta

import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.domain.enums import Direction, OptionType, PositionStatus, Strategy
from agentic.domain.models import Order, Position, utcnow
from agentic.store.db import Database
from agentic.store.positions import PositionStore

OCC = "SOFI260717P00017000"


def _pos(occ=OCC, credit=0.25):
    return Position(
        occ_symbol=occ, underlying="SOFI", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=17.0, expiration=utcnow().date() + timedelta(days=7), credit_received=credit,
        status=PositionStatus.OPEN,
    )


# --- Fix 1: re-selling the same contract keeps the closed episode --------------------------------

def test_reentry_preserves_closed_episode(tmp_path):
    store = PositionStore(Database(tmp_path / "p.db"))
    p1 = _pos()
    store.upsert(p1)
    id1 = p1.id
    store.set_status(id1, PositionStatus.CLOSED)     # the winning trade closes

    p2 = _pos()                                       # re-sell the SAME contract -> new episode
    store.upsert(p2)

    rows = [r for r in store.list_all() if r.occ_symbol == OCC]
    assert len(rows) == 2                             # both episodes survive (old behavior kept 1)
    assert len([r for r in store.list_open() if r.occ_symbol == OCC]) == 1
    assert any(r.id == id1 and r.status is PositionStatus.CLOSED for r in rows)


def test_repeated_upsert_of_open_does_not_duplicate(tmp_path):
    store = PositionStore(Database(tmp_path / "p.db"))
    p = _pos()
    store.upsert(p)
    store.upsert(p)                                   # monitor re-upserts each cycle
    store.upsert(p)
    assert len([r for r in store.list_all() if r.occ_symbol == OCC]) == 1


# --- Fix 2: paper positions survive a restart ----------------------------------------------------

def _open_order(occ="F260807P00012000"):
    return Order(decision_id="d1", position_id="pos1", occ_symbol=occ, quantity=2,
                 limit_price=0.22, is_paper=True, client_order_id=f"open-{occ}")


@pytest.mark.asyncio
async def test_paper_broker_persists_across_restart(tmp_path):
    path = tmp_path / "paper.json"
    b1 = PaperBroker(seed_positions=[], persist_path=path)
    await b1.submit_open_order(_open_order())
    assert path.exists()

    # Simulate a redeploy: a brand-new broker instance must restore the open position.
    b2 = PaperBroker(seed_positions=[], persist_path=path)
    opens = await b2.get_open_positions()
    assert any(p.occ_symbol == "F260807P00012000" for p in opens)


@pytest.mark.asyncio
async def test_paper_broker_close_drops_from_persisted(tmp_path):
    path = tmp_path / "paper.json"
    b1 = PaperBroker(seed_positions=[], persist_path=path)
    await b1.submit_open_order(_open_order())
    close = Order(decision_id="d1", position_id="pos1", occ_symbol="F260807P00012000",
                  quantity=2, limit_price=0.10, is_paper=True, client_order_id="close-1")
    await b1.submit_close_order(close)

    b2 = PaperBroker(seed_positions=[], persist_path=path)
    assert await b2.get_open_positions() == []        # closed -> not restored as open


def test_no_persist_path_stays_in_memory(tmp_path):
    # Default (tests) — no file written, seeds honored.
    b = PaperBroker(seed_positions=[])
    assert b._persist_path is None


# --- one-time cleanup ---------------------------------------------------------------------------

def test_purge_stale_removes_junk_keeps_real(tmp_path):
    db = Database(tmp_path / "c.db")
    store = PositionStore(db)
    # real closed win (has a FILLED order) -> keep
    win = _pos(occ="F260717P00013000")
    store.upsert(win)
    store.set_status(win.id, PositionStatus.CLOSED)
    db.conn.execute(
        "INSERT INTO orders (id,decision_id,position_id,client_order_id,occ_symbol,side,"
        "order_type,quantity,limit_price,filled_qty,status,is_paper) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("o1", "d", win.id, "c1", win.occ_symbol, "BUY_TO_CLOSE", "LIMIT", 1, 0.1, 1, "FILLED", 1),
    )
    db.conn.commit()
    junk = _pos(occ="T260717P00022000"); store.upsert(junk); store.set_status(junk.id, PositionStatus.CLOSED)
    seed = _pos(occ="MSFT260717P00400000"); seed.broker_position_id = "paper-msft-csp"
    store.upsert(seed); store.set_status(seed.id, PositionStatus.CLOSED)
    keep_open = _pos(occ="SOFI260717P00018500"); store.upsert(keep_open)
    keep_exp = _pos(occ="BULL260717P00007000"); store.upsert(keep_exp); store.set_status(keep_exp.id, PositionStatus.EXPIRED)

    removed = store.purge_stale()
    occs = {r.occ_symbol for r in store.list_all()}
    assert removed == 2
    assert "T260717P00022000" not in occs and "MSFT260717P00400000" not in occs   # junk + seed gone
    assert {"F260717P00013000", "SOFI260717P00018500", "BULL260717P00007000"} <= occs  # kept


def test_purge_stale_keeps_reconcile_detected_close_with_mark(tmp_path):
    """A reconcile-detected close has no FILLED order, but its last mark carries the estimated
    realized P&L that stats reports — purge must treat it as real history, not an artifact."""
    store = PositionStore(Database(tmp_path / "c.db"))
    est = _pos(occ="SMR260814P00008500", credit=0.34)
    est.current_mark = 0.18                            # last mark -> estimated realized +16
    store.upsert(est)
    store.set_status(est.id, PositionStatus.CLOSED)

    removed = store.purge_stale()
    assert removed == 0
    assert any(r.occ_symbol == "SMR260814P00008500" for r in store.list_all())


def test_purge_incomplete_journal(tmp_path):
    from agentic.store.trade_journal import TradeJournalStore
    db = Database(tmp_path / "j.db")
    tj = TradeJournalStore(db)
    for jid, status, pnl in [("j1", "closed", None), ("j2", "win", 16.0), ("j3", "expired", 25.0)]:
        db.conn.execute(
            "INSERT INTO trade_journal (id,occ_symbol,underlying,kind,contracts,strike,dte,context,"
            "status,realized_pnl,entered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (jid, "X260717P00001000", "X", "CSP", 1, 1.0, 7, "{}", status, pnl, utcnow().isoformat()),
        )
    db.conn.commit()
    assert tj.purge_incomplete() == 1                       # only the closed/no-pnl row
    assert {r.status for r in tj.recent()} == {"win", "expired"}
