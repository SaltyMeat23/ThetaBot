"""Entry pipeline end-to-end on the paper broker: scan -> screen -> size -> sell-to-open."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, Settings
from agentic.domain.enums import DecisionStatus, OptionType, OrderStatus, PositionStatus
from agentic.domain.models import utcnow
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_candidates import EntryCandidateStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.web.app import WebDeps, create_app


def _occ(underlying, exp, strike):
    return f"{underlying}{exp:%y%m%d}P{int(strike * 1000):08d}"


class StubChainData(MarketDataProvider):
    def __init__(self, chain, symbol="X"):
        self._chain = chain
        self._symbol = symbol

    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return self._chain if underlying == self._symbol else []


def _good_put_chain():
    exp = utcnow().date() + timedelta(days=35)
    occ = _occ("X", exp, 50)
    return [OptionContractQuote(
        occ_symbol=occ, underlying="X", option_id=None, option_type="put",
        strike=50.0, expiration=exp, bid=1.60, ask=1.70, mark=1.65,
        delta=-0.25, iv=0.45, open_interest=500, volume=50,
    )]


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    db = Database(tmp_path / "scan.db")
    audit, positions, decisions = AuditStore(db), PositionStore(db), DecisionStore(db)
    orders, entry_decisions = OrderStore(db), EntryDecisionStore(db)
    killswitch = KillSwitch(db, audit)
    entry_candidates = EntryCandidateStore(db)
    broker = PaperBroker(seed_positions=[], buying_power=100_000.0)
    market_data = StubChainData(_good_put_chain())
    settings = Settings(
        mode="paper", broker="paper",
        entry=EntryConfig(enabled=True, watchlist=["X"]),
    )
    executor = OrderExecutor(settings, broker, market_data, positions, orders, decisions,
                             audit, killswitch, entry_decisions=entry_decisions,
                             poll_interval_seconds=0.001)
    scanner = OpportunityScanner(settings, broker, market_data, entry_decisions, executor,
                                 audit, killswitch, entry_candidates=entry_candidates)
    return dict(scanner=scanner, broker=broker, entry_decisions=entry_decisions,
                positions=positions, orders=orders, audit=audit, killswitch=killswitch,
                decisions=decisions, signals=SignalStore(db), settings=settings,
                entry_candidates=entry_candidates)


@pytest.mark.asyncio
async def test_scan_enters_csp_on_paper(ctx):
    submitted = await ctx["scanner"].run_once()
    assert submitted == 1

    entries = ctx["entry_decisions"].recent()
    assert len(entries) == 1
    d = entries[0]
    assert d.underlying == "X" and d.contracts == 2 and d.status == DecisionStatus.DONE

    # The sell-to-open order filled...
    order = ctx["orders"].get_by_client_order_id(f"open-{d.id}")
    assert order is not None and order.status == OrderStatus.FILLED
    assert order.side == "SELL_TO_OPEN" and order.filled_qty == 2

    # ...and the broker now reports the new short put (reconcile would discover it).
    pos = await ctx["broker"].get_open_positions()
    new = [p for p in pos if p.underlying == "X"]
    assert len(new) == 1
    assert new[0].option_type is OptionType.PUT and new[0].direction.value == "SHORT"
    assert new[0].status is PositionStatus.OPEN


@pytest.mark.asyncio
async def test_scan_persists_candidate_disposition(ctx):
    await ctx["scanner"].run_once()
    rows = ctx["entry_candidates"].recent()
    assert len(rows) == 1
    r = rows[0]
    assert r["underlying"] == "X" and r["kind"] == "CSP"
    assert r["approved"] is True and r["reason"] == "approved" and r["contracts"] == 2

    # Surfaced on the candidate-log endpoint for offline refinement.
    deps = WebDeps(
        settings=ctx["settings"], signals=ctx["signals"], killswitch=ctx["killswitch"],
        approval_gate=None, audit=ctx["audit"], positions=ctx["positions"],
        orders=ctx["orders"], decisions=ctx["decisions"],
        entry_decisions=ctx["entry_decisions"], scanner=ctx["scanner"],
        entry_candidates=ctx["entry_candidates"],
    )
    client = TestClient(create_app(deps))
    log = client.get("/api/candidate-log").json()["candidates"]
    assert len(log) == 1 and log[0]["approved"] is True


@pytest.mark.asyncio
async def test_rejected_candidate_logged_with_reason(ctx):
    # Hold X already -> the screened candidate is rejected (one CSP per underlying), and the
    # rejection is persisted WITH the reason (the negative example the trade journal never sees).
    from agentic.domain.enums import Direction, Strategy
    from agentic.domain.models import Position
    from datetime import date
    ctx["broker"]._positions["Xheld"] = Position(  # paper broker holds an X short put
        occ_symbol="Xheld", underlying="X", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=45.0, expiration=date.today(), credit_received=1.0,
        status=PositionStatus.OPEN,
    )
    await ctx["scanner"].run_once()
    rows = [r for r in ctx["entry_candidates"].recent() if r["underlying"] == "X"]
    assert rows and rows[0]["approved"] is False
    assert "already holding X" in rows[0]["reason"]


@pytest.mark.asyncio
async def test_scan_is_idempotent(ctx):
    await ctx["scanner"].run_once()
    submitted2 = await ctx["scanner"].run_once()  # same contract, same day -> dedup
    assert submitted2 == 0
    assert len(ctx["entry_decisions"].recent()) == 1


@pytest.mark.asyncio
async def test_killswitch_blocks_scan(ctx):
    ctx["killswitch"].pause("test")
    assert await ctx["scanner"].run_once() == 0
    assert ctx["entry_decisions"].recent() == []


@pytest.mark.asyncio
async def test_dashboard_surfaces_candidates_and_entries(ctx):
    await ctx["scanner"].run_once()
    deps = WebDeps(
        settings=ctx["settings"], signals=ctx["signals"], killswitch=ctx["killswitch"],
        approval_gate=None, audit=ctx["audit"], positions=ctx["positions"],
        orders=ctx["orders"], decisions=ctx["decisions"],
        entry_decisions=ctx["entry_decisions"], scanner=ctx["scanner"],
    )
    client = TestClient(create_app(deps))
    cands = client.get("/api/candidates").json()
    assert len(cands["candidates"]) >= 1 and cands["candidates"][0]["underlying"] == "X"
    entries = client.get("/api/entry-decisions").json()["entries"]
    assert len(entries) == 1 and entries[0]["status"] == "DONE"
    assert entries[0]["id"]  # id exposed so it can be targeted by /control/heal-decision

    tech = client.get("/api/technicals").json()
    assert "X" in tech["symbols"] and "sma200" in tech["symbols"]["X"]  # trend inputs surfaced


@pytest.mark.asyncio
async def test_loss_breaker_freezes_new_entries(ctx):
    """Loss circuit breaker: a losing streak freezes NEW entries (no orders), without closing
    anything. Mirrors the killswitch guard but is a distinct, self-evaluating state."""
    class _LosingJournal:                       # 4 straight realized losers -> trips the streak rule
        def realized_since(self, since_iso):
            return (-40.0, 4)
        def resolved_pnls(self, limit=50):
            return [-8.0, -12.0, -5.0, -15.0, 20.0][:limit]
    ctx["scanner"].trade_journal = _LosingJournal()

    submitted = await ctx["scanner"].run_once()
    assert submitted == 0                                   # no new entries opened
    assert ctx["entry_decisions"].recent() == []            # nothing written
    brk = ctx["scanner"].last_breaker
    assert brk is not None and brk["tripped"] is True and "consecutive" in brk["reason"]
