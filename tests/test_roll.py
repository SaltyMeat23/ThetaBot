"""Roll defense: trigger conditions, target selection, and the two-leg execution."""
from datetime import date, timedelta

import pytest

from agentic.config import RollConfig, Settings
from agentic.domain.enums import Direction, OptionType, OrderStatus, Strategy
from agentic.domain.models import Order, Position, utcnow
from agentic.marketdata.quote import OptionContractQuote, OptionQuote
from agentic.services.roll import RollManager, select_roll_target, should_roll
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.decisions import DecisionStore
from agentic.store.entry_decisions import EntryDecisionStore

CFG = RollConfig(enabled=True, roll_dte=3, roll_delta=0.45, target_dte_min=7, target_dte_max=14,
                 min_net_credit=0.0, max_target_delta=0.35)
# Relative to the real today: try_roll computes DTE from utcnow(), so fixed calendar dates drift
# out of the target-DTE window as time passes (they did — this file was date-brittle).
TODAY = utcnow().date()


def _pos(strike=18.0, dte=2, delta=-0.5, credit=0.36):
    return Position(
        occ_symbol="SOFI260715P00018000", underlying="SOFI", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=strike, expiration=TODAY + timedelta(days=dte), credit_received=credit, delta=delta,
    )


def _put(strike, dte, mid, delta):
    return OptionContractQuote(
        occ_symbol=f"SOFI2607__P{int(strike*1000):08d}", underlying="SOFI", option_id=None,
        option_type="put", strike=strike, expiration=TODAY + timedelta(days=dte),
        bid=mid - 0.03, ask=mid + 0.03, mark=mid, delta=delta,
    )


# --- should_roll -------------------------------------------------------------------------------

def test_should_roll_when_tested_near_expiry():
    assert should_roll(_pos(dte=2, delta=-0.5), None, CFG) is not None


def test_no_roll_when_far_from_expiry():
    assert should_roll(_pos(dte=8, delta=-0.6), None, CFG) is None


def test_no_roll_when_not_tested():
    assert should_roll(_pos(dte=2, delta=-0.20), None, CFG) is None


def test_no_roll_when_disabled():
    assert should_roll(_pos(dte=2, delta=-0.6), None, RollConfig(enabled=False)) is None


# --- select_roll_target ------------------------------------------------------------------------

def test_selects_lowest_strike_credit_roll():
    chain = [
        _put(17.0, 10, 0.80, -0.25),   # net = 0.80 - 0.72 = +0.08, strike down -> preferred
        _put(18.0, 10, 0.98, -0.30),   # net +0.26 but higher strike
        _put(16.0, 10, 0.55, -0.15),   # net -0.17 -> rejected (not a credit)
        _put(19.0, 10, 1.30, -0.45),   # strike above current -> skipped
    ]
    picked = select_roll_target(chain, _pos(strike=18.0), current_cost=0.72, cfg=CFG, today=TODAY)
    assert picked is not None
    target, net = picked
    assert target.strike == 17.0 and net == pytest.approx(0.08)


def test_no_target_when_no_credit():
    chain = [_put(17.0, 10, 0.60, -0.25)]     # net = 0.60 - 0.72 < 0
    assert select_roll_target(chain, _pos(), current_cost=0.72, cfg=CFG, today=TODAY) is None


def test_skips_wrong_dte_and_too_tested():
    chain = [
        _put(17.0, 4, 0.90, -0.25),    # dte 4 < target_dte_min 7 -> skip
        _put(17.0, 10, 0.90, -0.50),   # |delta| 0.50 > max_target_delta 0.35 -> skip
    ]
    assert select_roll_target(chain, _pos(), current_cost=0.72, cfg=CFG, today=TODAY) is None


# --- two-leg execution -------------------------------------------------------------------------

class _FakeExec:
    def __init__(self):
        self.closed = None
        self.opened = None

    async def execute_close(self, position, decision, quote):
        self.closed = position.occ_symbol
        o = Order(decision_id=decision.id, position_id=position.id, occ_symbol=position.occ_symbol,
                  quantity=1, limit_price=0.72, is_paper=True, client_order_id="rc")
        o.status = OrderStatus.FILLED
        return o

    async def execute_open(self, decision, quote):
        self.opened = decision.occ_symbol
        o = Order(decision_id=decision.id, position_id="new", occ_symbol=decision.occ_symbol,
                  quantity=1, limit_price=0.80, is_paper=True, client_order_id="ro")
        o.status = OrderStatus.FILLED
        return o


class _MD:
    def __init__(self, chain):
        self._chain = chain

    async def get_chain(self, underlying):
        return self._chain


@pytest.mark.asyncio
async def test_try_roll_executes_both_legs(tmp_path):
    db = Database(tmp_path / "roll.db")
    audit = AuditStore(db)
    ex = _FakeExec()
    md = _MD([_put(17.0, 10, 0.80, -0.25)])       # a credit roll exists
    rm = RollManager(Settings(roll=CFG), broker=None, market_data=md, executor=ex,
                     decisions=DecisionStore(db), entry_decisions=EntryDecisionStore(db),
                     audit=audit, notifier=None)
    pos = _pos(dte=2, delta=-0.5)
    quote = OptionQuote(occ_symbol=pos.occ_symbol, bid=0.68, ask=0.72, mark=0.70, delta=-0.5)
    rolled = await rm.try_roll(pos, quote)
    assert rolled is True
    assert ex.closed == "SOFI260715P00018000"     # closed the tested put
    assert ex.opened.endswith("P00017000")        # opened the $17 further-dated put


@pytest.mark.asyncio
async def test_try_roll_holds_when_no_credit(tmp_path):
    db = Database(tmp_path / "roll2.db")
    ex = _FakeExec()
    md = _MD([_put(17.0, 10, 0.60, -0.25)])        # net negative -> no roll
    rm = RollManager(Settings(roll=CFG), broker=None, market_data=md, executor=ex,
                     decisions=DecisionStore(db), entry_decisions=EntryDecisionStore(db),
                     audit=AuditStore(db), notifier=None)
    pos = _pos(dte=2, delta=-0.5)
    quote = OptionQuote(occ_symbol=pos.occ_symbol, bid=0.68, ask=0.72, mark=0.70, delta=-0.5)
    assert await rm.try_roll(pos, quote) is False
    assert ex.closed is None and ex.opened is None  # never touched the position


class _CountingNotifier:
    def __init__(self):
        self.sent = []

    async def send(self, title, message, *, priority="normal", actions=None):
        self.sent.append((title, message))


@pytest.mark.asyncio
async def test_no_credit_roll_notifies_once_per_day(tmp_path):
    # A tested put with no acceptable roll is re-evaluated every monitor cycle. The "no credit
    # roll, riding to assignment" alert must fire ONCE per position per day, not on every poll —
    # this is the notification-spam fix.
    db = Database(tmp_path / "roll3.db")
    notifier = _CountingNotifier()
    md = _MD([_put(17.0, 10, 0.60, -0.25)])        # net negative -> no roll available
    rm = RollManager(Settings(roll=CFG), broker=None, market_data=md, executor=_FakeExec(),
                     decisions=DecisionStore(db), entry_decisions=EntryDecisionStore(db),
                     audit=AuditStore(db), notifier=notifier)
    pos = _pos(dte=2, delta=-0.5)
    quote = OptionQuote(occ_symbol=pos.occ_symbol, bid=0.68, ask=0.72, mark=0.70, delta=-0.5)

    for _ in range(5):                              # five monitor cycles, same position, same day
        assert await rm.try_roll(pos, quote) is False
    assert len(notifier.sent) == 1                  # exactly one alert, not five
    assert "No credit roll" in notifier.sent[0][0]
