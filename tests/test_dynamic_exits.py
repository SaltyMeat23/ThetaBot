"""Dynamic exits: fixed & trailing take-profit, stop-loss triggers, monitor peak tracking."""
from datetime import date, timedelta

import pytest

from agentic.config import Settings
from agentic.domain.enums import Direction, OptionType, Strategy
from agentic.domain.models import Position, utcnow
from agentic.marketdata.quote import OptionQuote
from agentic.rules.profit_target import ProfitTargetRule
from agentic.rules.stop_loss import StopLossRule
from agentic.services.killswitch import KillSwitch
from agentic.services.monitor import MonitorLoop
from agentic.store.audit import AuditStore
from agentic.store.db import Database
from agentic.store.positions import PositionStore


def _pos(credit=1.0, peak=0.0, delta=-0.2):
    return Position(
        occ_symbol="SOFI260717P00017000", underlying="SOFI", option_type=OptionType.PUT,
        strategy=Strategy.CASH_SECURED_PUT, direction=Direction.SHORT, quantity=1,
        strike=17.0, expiration=utcnow().date() + timedelta(days=7), credit_received=credit,
        peak_profit_pct=peak, delta=delta,
    )


def _q(ask, bid=None, delta=-0.2):
    bid = ask - 0.05 if bid is None else bid
    return OptionQuote(occ_symbol="SOFI260717P00017000", bid=bid, ask=ask, mark=(ask + bid) / 2,
                       delta=delta)


NOW = utcnow()


# --- fixed take-profit (backward compatible) ----------------------------------------------------

def test_fixed_profit_closes_at_target():
    r = ProfitTargetRule("profit-50", False, {"profit_pct": 0.5})
    assert r.evaluate(_pos(credit=1.0), _q(ask=0.5, bid=0.45), NOW) is not None   # 50% captured
    assert r.evaluate(_pos(credit=1.0), _q(ask=0.6, bid=0.55), NOW) is None       # only 40%


# --- trailing take-profit -----------------------------------------------------------------------

def test_trailing_not_armed_holds():
    r = ProfitTargetRule("trail", False, {"profit_pct": 0.5, "trailing": True, "trail_gap": 0.2})
    # profit only 40%, peak 40% -> not yet armed -> hold
    assert r.evaluate(_pos(credit=1.0, peak=0.4), _q(ask=0.6, bid=0.55), NOW) is None


def test_trailing_holds_while_running():
    r = ProfitTargetRule("trail", False, {"profit_pct": 0.5, "trailing": True, "trail_gap": 0.2})
    # peak 80%, currently 75% (still within the 20-pt trail of the 80% peak) -> hold
    assert r.evaluate(_pos(credit=1.0, peak=0.8), _q(ask=0.25, bid=0.20), NOW) is None


def test_trailing_closes_on_pullback():
    r = ProfitTargetRule("trail", False, {"profit_pct": 0.5, "trailing": True, "trail_gap": 0.2})
    # peak 80%, pulled back to 55% (<= exit level max(0.5, 0.8-0.2)=0.6) -> close
    d = r.evaluate(_pos(credit=1.0, peak=0.8), _q(ask=0.45, bid=0.40), NOW)
    assert d is not None and "Trailing" in d.reason


def test_trailing_floors_at_arm():
    r = ProfitTargetRule("trail", False, {"profit_pct": 0.5, "trailing": True, "trail_gap": 0.2})
    # peak == arm (50%), currently 50% -> exit level floors at 50% -> close (never below arm)
    assert r.evaluate(_pos(credit=1.0, peak=0.5), _q(ask=0.5, bid=0.45), NOW) is not None


# --- stop-loss ----------------------------------------------------------------------------------

def test_stop_loss_on_loss_multiple():
    r = StopLossRule("stop", False, {"loss_mult": 2.0, "delta_stop": 0.5})
    d = r.evaluate(_pos(credit=1.0), _q(ask=2.1, bid=2.0, delta=-0.3), NOW)  # cost 2.1 >= 2x
    assert d is not None and "Stop-loss" in d.reason


def test_stop_loss_on_delta():
    r = StopLossRule("stop", False, {"loss_mult": 2.0, "delta_stop": 0.5})
    d = r.evaluate(_pos(credit=1.0), _q(ask=1.4, bid=1.3, delta=-0.55), NOW)  # |delta| >= 0.5
    assert d is not None and "Delta" in d.reason


def test_stop_loss_holds_when_fine():
    r = StopLossRule("stop", False, {"loss_mult": 2.0, "delta_stop": 0.5})
    assert r.evaluate(_pos(credit=1.0), _q(ask=1.2, bid=1.1, delta=-0.25), NOW) is None


# --- monitor peak tracking ----------------------------------------------------------------------

class _Broker:
    def __init__(self, positions):
        self._p = positions

    async def get_open_positions(self):
        return self._p


class _MD:
    def __init__(self, quote):
        self._q = quote

    async def get_quote(self, pos):
        return self._q


@pytest.mark.asyncio
async def test_monitor_tracks_and_persists_peak(tmp_path):
    db = Database(tmp_path / "m.db")
    positions = PositionStore(db)
    audit = AuditStore(db)
    pos = _pos(credit=1.0)
    mon = MonitorLoop(Settings(broker="paper", market_data="paper"),
                      _Broker([pos]), _MD(_q(ask=0.5, bid=0.45)), positions, audit,
                      KillSwitch(db, audit))
    await mon.run_once()                       # 50% captured
    assert positions.get_by_occ(pos.occ_symbol).peak_profit_pct == pytest.approx(0.5)

    # A worse quote next cycle must NOT lower the high-water mark.
    mon.market_data = _MD(_q(ask=0.7, bid=0.65))   # only 30%
    await mon.run_once()
    assert positions.get_by_occ(pos.occ_symbol).peak_profit_pct == pytest.approx(0.5)


# --- rules hot-reload ---------------------------------------------------------------------------

def test_rules_engine_hot_reload():
    from agentic.config import RuleConfig
    from agentic.rules.engine import RulesEngine
    cfgs = [RuleConfig(name="p", rule_type="PROFIT_TARGET", requires_approval=False,
                       params={"profit_pct": 0.5})]
    eng = RulesEngine.from_configs(cfgs)
    assert [r.name for r in eng.rules] == ["p"]
    assert eng.refresh(cfgs) is False                      # unchanged -> no rebuild
    cfgs2 = cfgs + [RuleConfig(name="s", rule_type="STOP_LOSS", requires_approval=False,
                               params={"loss_mult": 2.0})]
    assert eng.refresh(cfgs2) is True                      # changed -> rebuilt live
    assert {r.name for r in eng.rules} == {"p", "s"}
