"""Tax reserve sweep, capital-aware tiers, holdings helpers, assignment clock, and the surfaces."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from agentic.brokers.paper_broker import PaperBroker
from agentic.brokers.robinhood_mcp import RobinhoodMCPBroker
from agentic.config import EntryCriteria, Settings, TaxReserveConfig
from agentic.domain.models import EquityHolding
from agentic.entry.screener import screen_candidates
from agentic.marketdata.quote import OptionContractQuote
from agentic.services import holdings as hl
from agentic.services.killswitch import KillSwitch
from agentic.services.reporting import reserve_line
from agentic.services.tax_reserve import TaxReserveLoop, period_key, scheduled_period_end
from agentic.services.tiers import next_unlock, ready_to_add
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore
from agentic.store.signals import SignalStore
from agentic.store.tax_reserve import TaxReserveStore
from agentic.web.app import WebDeps, create_app

ET = ZoneInfo("America/New_York")


def _fri(h=15, m=41, week_offset=0):
    """A Friday at h:m ET (2026-09-11 is a Friday)."""
    return datetime(2026, 9, 11, h, m, tzinfo=ET) + timedelta(days=7 * week_offset)


# --- ledger ---------------------------------------------------------------------------------------

def test_store_record_dedup_and_totals(tmp_path):
    st = TaxReserveStore(Database(tmp_path / "tr.db"))
    assert st.record(period_start="a", period_end="p1", net_realized=500, carry_in=0, carry_out=0,
                     amount_due=100, status="filled", symbol="sgov", dollar_amount=100, shares=0.99, fill_price=101)
    assert st.record(period_start="a", period_end="p1", net_realized=1, carry_in=0, carry_out=0,
                     amount_due=0, status="skipped", symbol="SGOV") is False        # same period -> no-op
    assert st.record(period_start="p1", period_end="p2", net_realized=-300, carry_in=0, carry_out=-300,
                     amount_due=0, status="skipped", symbol="SGOV")
    assert st.has_period("p1") and not st.has_period("p3")
    assert st.last()["period_end"] == "p2" and st.last()["symbol"] == "SGOV"
    t = st.totals()
    assert t["periods"] == 2 and t["sweeps"] == 1 and t["swept_dollars"] == 100 and t["carry"] == -300
    assert abs(t["shares"] - 0.99) < 1e-9 and t["net_realized"] == 200


# --- schedule -------------------------------------------------------------------------------------

def test_schedule_helpers():
    cur = _fri(15, 41)
    sched = scheduled_period_end(cur, 4, 15, 40)
    assert sched == _fri(15, 40)
    # a minute before the time on Friday -> last week's instant
    assert scheduled_period_end(_fri(15, 39), 4, 15, 40) == _fri(15, 40, -1)
    # Monday after -> still last Friday's instant (caught up at the next regular-hours poll)
    assert scheduled_period_end(_fri(15, 41) + timedelta(days=3), 4, 15, 40) == _fri(15, 40)
    assert period_key(sched).endswith("+00:00") and period_key(sched).startswith("2026-09-11T19:40")


# --- loop -----------------------------------------------------------------------------------------

class _Journal:
    def __init__(self, sums):
        self.sums = sums            # period_start -> (sum, n)
        self.calls = []

    def realized_since(self, since_iso):
        self.calls.append(since_iso)
        return self.sums.get(since_iso, (0.0, 0))


class _Notes:
    def __init__(self):
        self.sent = []

    async def send(self, title, message, priority="normal"):
        self.sent.append((title, priority))


class _MD:
    async def get_underlying_price(self, sym):
        return 100.5


def _loop(tmp_path, journal, *, broker=None, settings=None, notes=None):
    db = Database(tmp_path / "loop.db")
    audit = AuditStore(db)
    settings = settings or Settings(mode="paper", tax_reserve=TaxReserveConfig(enabled=True, dry_run=False))
    return TaxReserveLoop(settings, broker or PaperBroker(seed_positions=[], buying_power=10_000.0), _MD(),
                          journal, TaxReserveStore(db), audit, KillSwitch(db, audit), notifier=notes), db


@pytest.mark.asyncio
async def test_loop_buys_20pct_of_net_gain_on_paper_and_dedups(tmp_path):
    key0 = "1970-01-01T00:00:00+00:00"
    j = _Journal({key0: (500.0, 6)})
    notes = _Notes()
    broker = PaperBroker(seed_positions=[], buying_power=10_000.0)
    loop, db = _loop(tmp_path, j, broker=broker, notes=notes)
    r = await loop.run_once(now=_fri(15, 41))
    assert r["status"] == "filled" and r["amount"] == 100.0 and abs(r["shares"] - 100 / 100.5) < 1e-6
    held = await broker.get_equity_positions()
    assert held and held[0].symbol == "SGOV" and abs(held[0].quantity - 100 / 100.5) < 1e-6
    assert await broker.get_buying_power() == 9_900.0
    assert notes.sent and notes.sent[0][0].startswith("Tax reserve: bought")
    # the same period is never swept twice, even on a later poll the same week
    assert await loop.run_once(now=_fri(15, 55)) is None
    row = loop.store.last()
    assert row["status"] == "filled" and row["dollar_amount"] == 100.0 and row["carry_out"] == 0.0
    # status endpoint-style read
    st = loop.status(now=_fri(15, 56))
    assert st["next_sweep_at"].startswith("2026-09-18T15:40")


@pytest.mark.asyncio
async def test_loop_carries_losses_forward_and_skips_small_amounts(tmp_path):
    key0 = "1970-01-01T00:00:00+00:00"
    p1 = period_key(_fri(15, 40))
    p2 = period_key(_fri(15, 40, 1))
    j = _Journal({key0: (-300.0, 4), p1: (500.0, 5), p2: (10.0, 1)})
    loop, _ = _loop(tmp_path, j)
    r1 = await loop.run_once(now=_fri(15, 41))            # losing week
    assert r1["status"] == "skipped" and r1["carry_out"] == -300.0
    r2 = await loop.run_once(now=_fri(15, 41, 1))         # +500 week, minus the carried -300
    assert r2["status"] == "filled" and r2["amount"] == 40.0
    r3 = await loop.run_once(now=_fri(15, 41, 2))         # +10 -> $2 due, under the $5 minimum
    assert r3["status"] == "skipped" and r3["carry_out"] == 10.0
    assert j.calls == [key0, p1, p2]                      # each period starts where the last ended
    assert loop.store.totals()["sweeps"] == 1


@pytest.mark.asyncio
async def test_loop_waits_for_market_hours_and_respects_dry_run_and_killswitch(tmp_path):
    key0 = "1970-01-01T00:00:00+00:00"
    j = _Journal({key0: (500.0, 6)})
    # outside regular hours -> nothing yet, still due
    loop, db = _loop(tmp_path, j)
    assert await loop.run_once(now=_fri(16, 30)) is None and loop.store.last() is None
    # kill switch paused -> deferred
    loop.killswitch.pause("test")
    assert await loop.run_once(now=_fri(15, 41)) is None and loop.store.last() is None
    loop.killswitch.resume()
    # live mode on a real broker with dry_run -> ledger row, no order
    class _Real(PaperBroker):
        def capabilities(self):
            c = super().capabilities()
            c.is_paper = False
            return c
    notes = _Notes()
    live = Settings(mode="live", i_understand_live_trading=True,
                    tax_reserve=TaxReserveConfig(enabled=True, dry_run=True))
    loop2, _ = _loop(tmp_path / "b", j, broker=_Real(seed_positions=[], buying_power=10_000.0), settings=live, notes=notes)
    r = await loop2.run_once(now=_fri(15, 41))
    assert r["status"] == "dry_run" and r["amount"] == 100.0
    assert loop2.store.last()["status"] == "dry_run" and notes.sent[0][0].startswith("Tax reserve (dry run)")
    # disabled -> nothing
    off = Settings(mode="paper", tax_reserve=TaxReserveConfig(enabled=False))
    loop3, _ = _loop(tmp_path / "c", j, settings=off)
    assert await loop3.run_once(now=_fri(15, 41)) is None


# --- brokers --------------------------------------------------------------------------------------

def test_robinhood_equity_order_args_and_parse():
    b = RobinhoodMCPBroker(account_number="1234567890")
    args = b._build_equity_order_args(symbol="sgov", dollar_amount=123.456, ref_id="ref-1")
    assert args == {"account_number": "1234567890", "symbol": "SGOV", "side": "buy", "type": "market",
                    "dollar_amount": "123.46", "time_in_force": "gfd", "market_hours": "regular_hours",
                    "ref_id": "ref-1"}
    p = RobinhoodMCPBroker._parse_equity_order({"id": "o1", "state": "filled", "cumulative_quantity": "0.9876",
                                                "average_price": "101.20", "executed_notional": {"amount": "99.95"}})
    assert p == {"order_id": "o1", "status": "filled", "shares": 0.9876, "avg_price": 101.2, "dollars": 99.95, "raw_state": "filled"}
    assert RobinhoodMCPBroker._parse_equity_order({"id": "o2", "state": "queued"})["status"] == "submitted"
    assert RobinhoodMCPBroker._parse_equity_order({"id": "o3", "state": "rejected"})["status"] == "rejected"
    assert b.capabilities().supports_equity_orders is False          # tool not resolved when not connected


@pytest.mark.asyncio
async def test_robinhood_equity_order_refuses_sells_and_non_market():
    b = RobinhoodMCPBroker(account_number="1234567890")
    with pytest.raises(RuntimeError):
        await b.submit_equity_order(symbol="SGOV", side="sell", dollar_amount=10)
    with pytest.raises(RuntimeError):
        await b.submit_equity_order(symbol="SGOV", side="buy", quantity=1)


@pytest.mark.asyncio
async def test_paper_equity_order_merges_holdings():
    pb = PaperBroker(seed_positions=[], buying_power=1_000.0, holdings=[EquityHolding("SGOV", 1.0, 100.0)])
    r = await pb.submit_equity_order(symbol="SGOV", side="buy", dollar_amount=50.0, price_hint=101.0)
    assert r["status"] == "filled" and abs(r["shares"] - 50 / 101) < 1e-4
    h = (await pb.get_equity_positions())[0]
    assert abs(h.quantity - (1 + 50 / 101)) < 1e-4 and 100.0 < h.average_cost < 101.0
    assert await pb.get_buying_power() == 950.0
    assert pb.capabilities().supports_equity_orders is True
    with pytest.raises(RuntimeError):
        await pb.submit_equity_order(symbol="SGOV", side="sell", dollar_amount=10.0)


# --- tiers ----------------------------------------------------------------------------------------

def test_tiers_ready_and_next_unlock():
    tiers = {"T": {"min_collateral": 2500, "note": "AT&T", "per_ticker": {"min_annualized_yield": 0.2}},
             "KO": {"min_collateral": 8900}, "NVDA": {"min_collateral": 22000}}
    ready = ready_to_add(tiers, ["ONDS", "t"], 20_000.0, 0.25, {"KO": 70.0})
    assert [r["symbol"] for r in ready] == []                      # T already on the list; KO $7,000 > $5,000 cap
    ready = ready_to_add(tiers, ["ONDS"], 20_000.0, 0.25, {"KO": 70.0})
    assert [r["symbol"] for r in ready] == ["T"] and ready[0]["per_ticker"] == {"min_annualized_yield": 0.2}
    ready = ready_to_add(tiers, ["ONDS"], 40_000.0, 0.25, {"KO": 70.0})
    assert [r["symbol"] for r in ready] == ["T", "KO"] and ready[1]["price_source"] == "live"
    nxt = next_unlock(tiers, ["ONDS", "T"], 20_000.0, 0.25, {"KO": 70.0})
    assert nxt["symbol"] == "KO" and nxt["account_value_needed"] == 28_000.0
    assert ready_to_add(tiers, [], None, 0.25) == [] and next_unlock({}, [], 1, 0.25) is None


# --- holdings helpers + assignment clock ---------------------------------------------------------

def _je(underlying, status, closed_at):
    return SimpleNamespace(underlying=underlying, status=status, closed_at=closed_at)


def test_holdings_helpers_and_clock():
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    rows = [_je("SMR", "assigned", "2026-07-01T20:00:00+00:00"), _je("SMR", "win", "2026-08-01T20:00:00+00:00"),
            _je("SMR", "assigned", datetime(2026, 8, 1, 20, tzinfo=timezone.utc)), _je("BULL", "assigned", "2026-09-10T20:00:00+00:00")]
    assert hl.held_since(rows, "smr") == datetime(2026, 8, 1, 20, tzinfo=timezone.utc)
    assert hl.days_held(rows, "SMR", now) == 47 and hl.days_held(rows, "ONDS", now) is None
    s = Settings(); assert hl.reserve_symbols(s) == {"SGOV"}
    held = [EquityHolding("SGOV", 3.2, 100.0), EquityHolding("SMR", 100, 42.0)]
    assert [h.symbol for h in hl.tradable_holdings(held, {"SGOV"})] == ["SMR"]
    crit = EntryCriteria(cc_below_basis_after_days=42)
    assert hl.below_basis_allowed(held[1], crit, rows, now, price=9.0) == (True, 47)     # under water, 47d >= 42
    assert hl.below_basis_allowed(held[1], crit, rows, now, price=50.0) == (False, 47)   # above basis -> floor
    assert hl.below_basis_allowed(EquityHolding("BULL", 100, 12.5), crit, rows, now, price=7.0) == (False, 7)
    assert hl.below_basis_allowed(held[1], EntryCriteria(), rows, now, price=9.0) == (False, None)   # clock off
    assert hl.below_basis_allowed(EquityHolding("ONDS", 100, 8.0), crit, rows, now, price=5.0) == (False, None)  # unknown since


def test_screener_strike_ceiling_and_delta_bypass():
    from datetime import date
    exp = date.today() + timedelta(days=10)
    def call(K, delta):
        return OptionContractQuote(occ_symbol=f"X{K}", underlying="X", option_id=None, option_type="call", strike=K,
                                   expiration=exp, bid=0.40, ask=0.50, mark=0.45, delta=delta, iv=0.6,
                                   open_interest=500, volume=50)
    chain = [call(10.0, 0.45), call(10.5, 0.30), call(11.0, 0.12), call(12.0, 0.05)]
    crit = EntryCriteria(delta_min=0.2, delta_max=0.3, dte_min=7, dte_max=14, min_annualized_yield=0.0,
                         min_open_interest=1, min_volume=1, max_spread_pct=0.5)
    assert [c.strike for c in screen_candidates("X", chain, crit, option_type="call")] == [10.5]
    # 5-10% band above a $10 spot, delta ignored -> 10.5 and 11.0 (12.0 is above the ceiling)
    got = screen_candidates("X", chain, crit, option_type="call", strike_floor=10.5, strike_ceiling=11.0, ignore_delta=True)
    assert sorted(c.strike for c in got) == [10.5, 11.0]


# --- surfaces -------------------------------------------------------------------------------------

def _client(tmp_path):
    db = Database(tmp_path / "web.db")
    audit = AuditStore(db)
    store = TaxReserveStore(db)
    settings = Settings(mode="paper", tax_reserve=TaxReserveConfig(enabled=True))
    loop = TaxReserveLoop(settings, PaperBroker(seed_positions=[], buying_power=1000.0), _MD(),
                          _Journal({"1970-01-01T00:00:00+00:00": (250.0, 3)}), store, audit, KillSwitch(db, audit))
    deps = WebDeps(settings=settings, signals=SignalStore(db), killswitch=KillSwitch(db, audit), approval_gate=None,
                   audit=audit, positions=PositionStore(db), orders=OrderStore(db), decisions=DecisionStore(db),
                   tax_reserve=loop, tax_reserve_store=store)
    return TestClient(create_app(deps))


def test_endpoints_and_page_markers(tmp_path):
    c = _client(tmp_path)
    d = c.get("/api/tax-reserve").json()
    assert d["config"]["symbol"] == "SGOV" and d["config"]["enabled"] is True
    assert d["status"]["pending_net_since_last"] == 250.0 and d["status"]["would_sweep"] == 50.0
    assert d["totals"]["sweeps"] == 0 and d["recent"] == []
    t = c.get("/api/tiers").json()
    assert t["ready"] == [] and "tiers" in t
    page = c.get("/dashboard").text
    for m in ('id="reserve-card"', 'id="tiers-card"', 'id="tn-tr-on"', 'id="tn-tr-save"', "loadReserve", "loadTiers",
              "Tax reserve", "Ready to add", "<th>Note</th>"):
        assert m in page, m
    assert c.get("/api/holdings").json() == {"holdings": []}
    # tax_reserve is hot-editable
    r = c.post("/api/config", json={"tax_reserve": {"pct": 0.25, "dry_run": False}})
    assert r.status_code == 200 and r.json()["editable"]["tax_reserve"]["pct"] == 0.25


def test_reserve_line_for_reports(tmp_path):
    db = Database(tmp_path / "r.db")
    store = TaxReserveStore(db)
    store.record(period_start="a", period_end="p1", net_realized=500, carry_in=0, carry_out=0, amount_due=100,
                 status="filled", symbol="SGOV", dollar_amount=100)
    sc = SimpleNamespace(last_reserve={"value": 101.2, "symbol": "SGOV"}, tax_reserve_store=store,
                         settings=Settings(tax_reserve=TaxReserveConfig(enabled=True, dry_run=True)))
    line = reserve_line(sc)
    assert line.startswith("Tax reserve: $101 in SGOV") and "swept $100 over 1 sweep(s)" in line and line.endswith("DRY RUN")
    assert reserve_line(SimpleNamespace(settings=Settings())) is None                 # disabled -> no line


# --- scanner integration: the reserve is netted out of sizing and never written against --------

@pytest.mark.asyncio
async def test_scanner_nets_reserve_and_excludes_it_from_calls(tmp_path, monkeypatch):
    from tests.test_entry_intelligence import CRIT, _scanner
    monkeypatch.setattr("agentic.services.scanner.is_market_hours", lambda: True)
    closes = [10 + i * 0.1 for i in range(260)]                       # StubMD price = closes[-1] for any symbol
    sc, *_ = _scanner(tmp_path, closes, CRIT)
    sc.broker._holdings = [EquityHolding("SGOV", 10.0, 100.0), EquityHolding("X", 100, 60.0)]
    gross_av = await sc.broker.get_account_value()
    await sc.run_once()
    px = closes[-1]
    assert sc.last_reserve["symbol"] == "SGOV" and sc.last_reserve["shares"] == 10.0
    assert abs(sc.last_reserve["value"] - 10 * px) < 0.01
    assert abs(sc.last_account_value - (gross_av - 10 * px)) < 0.01          # sizer never sees the reserve
    assert "SGOV" not in sc.last_cc_clock and "X" in sc.last_cc_clock        # calls only on tradable shares
    assert sc.last_cc_clock["X"]["below_basis_allowed"] is False              # clock off by default
