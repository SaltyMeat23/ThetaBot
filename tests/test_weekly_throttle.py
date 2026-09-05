"""Weekly premium throttle: over-budget entries park for approval, then approve/reject/expire."""
from datetime import timedelta

import pytest

from agentic.brokers.paper_broker import PaperBroker
from agentic.config import EntryConfig, Settings
from agentic.domain.enums import DecisionStatus, OrderStatus
from agentic.domain.models import EntryDecision, utcnow
from agentic.entry.risk import ApprovedEntry
from agentic.entry.screener import EntryCandidate
from agentic.marketdata.base import MarketDataProvider
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.executor import OrderExecutor
from agentic.services.killswitch import KillSwitch
from agentic.services.scanner import OpportunityScanner
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore
from agentic.store.orders import OrderStore
from agentic.store.positions import PositionStore

EXP = utcnow().date() + timedelta(days=8)
OCC = "ONDS260724P00006000"


def _quote():
    return OptionContractQuote(
        occ_symbol=OCC, underlying="ONDS", option_id=None, option_type="put", strike=6.0,
        expiration=EXP, bid=0.12, ask=0.14, mark=0.13, delta=-0.22, iv=0.5,
        open_interest=500, volume=50, as_of=utcnow())


class OneChain(MarketDataProvider):
    async def get_quote(self, position):
        return None

    async def get_chain(self, underlying):
        return [_quote()] if underlying == "ONDS" else []


def _candidate():
    return EntryCandidate(
        underlying="ONDS", occ_symbol=OCC, option_id=None, strike=6.0, expiration=EXP, dte=8,
        delta=-0.22, iv=0.5, premium=0.13, ror=2.2, annualized_ror=100.0, max_risk=600.0,
        break_even=5.87, open_interest=500, volume=50, score=100.0)


def _approved():
    return ApprovedEntry(candidate=_candidate(), contracts=1, collateral=600.0)


@pytest.fixture()
def sc(tmp_path):
    db = Database(tmp_path / "wt.db")
    audit = AuditStore(db)
    positions, decisions = PositionStore(db), DecisionStore(db)
    orders, entry_decisions = OrderStore(db), EntryDecisionStore(db)
    killswitch = KillSwitch(db, audit)
    broker = PaperBroker(seed_positions=[], buying_power=5000.0)
    settings = Settings(mode="paper", broker="paper", entry=EntryConfig(
        enabled=True, watchlist=["ONDS"], weekly_premium_target_pct=0.02))  # 2% of $5000 = $100
    ex = OrderExecutor(settings, broker, OneChain(), positions, orders, decisions, audit,
                       killswitch, entry_decisions=entry_decisions, poll_interval_seconds=0.001)
    scanner = OpportunityScanner(settings, broker, OneChain(), entry_decisions, ex, audit, killswitch)
    return scanner, entry_decisions


def _prime_collected(entry_decisions, dollars):
    """Insert a DONE entry that already collected `dollars` of premium this week."""
    prem = dollars / 100.0
    d = EntryDecision(underlying="ONDS", occ_symbol="ONDS_PRIOR", option_id=None, strike=6.0,
                      expiration=EXP, contracts=1, premium=prem, rule_name="csp-screener",
                      reason="prior", dedup_key="prior")
    entry_decisions.insert_if_new(d)
    entry_decisions.set_status(d.id, DecisionStatus.DONE)


@pytest.mark.asyncio
async def test_under_budget_executes(sc):
    scanner, ed = sc
    n = await scanner._submit([_approved()], {OCC: _quote()}, "csp-screener", "",
                              review=True, account_value=5000.0)
    assert n == 1                                        # executed, not parked
    d = ed.recent(1)[0]
    assert d.status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_over_budget_parks_for_approval(sc):
    scanner, ed = sc
    _prime_collected(ed, 150.0)                          # $150 collected > $100 target
    n = await scanner._submit([_approved()], {OCC: _quote()}, "csp-screener", "",
                              review=True, account_value=5000.0)
    assert n == 0                                        # nothing auto-executed
    parked = ed.list_by_status(DecisionStatus.AWAITING_APPROVAL)
    assert len(parked) == 1 and parked[0].occ_symbol == OCC


@pytest.mark.asyncio
async def test_approve_parked_entry_executes(sc):
    scanner, ed = sc
    _prime_collected(ed, 150.0)
    await scanner._submit([_approved()], {OCC: _quote()}, "csp-screener", "",
                          review=True, account_value=5000.0)
    parked = ed.list_by_status(DecisionStatus.AWAITING_APPROVAL)[0]
    res = await scanner.approve_parked_entry(parked.id)
    assert res["ok"] and res["status"] == "executed"
    assert ed.get(parked.id).status == DecisionStatus.DONE


@pytest.mark.asyncio
async def test_reject_parked_entry(sc):
    scanner, ed = sc
    _prime_collected(ed, 150.0)
    await scanner._submit([_approved()], {OCC: _quote()}, "csp-screener", "",
                          review=True, account_value=5000.0)
    parked = ed.list_by_status(DecisionStatus.AWAITING_APPROVAL)[0]
    res = await scanner.reject_parked_entry(parked.id)
    assert res["ok"] and ed.get(parked.id).status == DecisionStatus.REJECTED


@pytest.mark.asyncio
async def test_expired_approval_cannot_execute(sc):
    scanner, ed = sc
    # An AWAITING_APPROVAL entry whose window has passed.
    d = EntryDecision(underlying="ONDS", occ_symbol=OCC, option_id=None, strike=6.0,
                      expiration=EXP, contracts=1, premium=0.13, rule_name="csp-screener",
                      reason="old", dedup_key="old",
                      created_at=utcnow() - timedelta(seconds=99999))
    ed.insert_if_new(d)
    ed.set_status(d.id, DecisionStatus.AWAITING_APPROVAL)
    assert scanner.expire_stale_entry_approvals() == 1
    assert ed.get(d.id).status == DecisionStatus.EXPIRED
    res = await scanner.approve_parked_entry(d.id)          # too late
    assert not res["ok"] and res["status"] == "not_pending"


def test_premium_collected_since_counts_done_only(sc):
    scanner, ed = sc
    _prime_collected(ed, 150.0)                             # DONE -> counts
    pending = EntryDecision(underlying="ONDS", occ_symbol="ONDS_PENDING", option_id=None,
                            strike=6.0, expiration=EXP, contracts=1, premium=2.0,
                            rule_name="csp-screener", reason="pending", dedup_key="pending")
    ed.insert_if_new(pending)                               # PROPOSED -> must NOT count
    since = scanner._week_start_iso()
    assert ed.premium_collected_since(since) == 150.0


def test_entry_action_token_requires_secret_and_is_per_decision(monkeypatch):
    from agentic.config import entry_action_token
    monkeypatch.delenv("CONTROL_TOKEN", raising=False)
    assert entry_action_token("abc") is None                     # no secret -> no token -> endpoint refuses
    monkeypatch.setenv("CONTROL_TOKEN", "s3cr3t")
    t1, t2 = entry_action_token("abc"), entry_action_token("abc")
    assert t1 == t2 and len(t1) == 64                            # deterministic sha256 hex
    assert entry_action_token("xyz") != t1                       # bound to the decision id
