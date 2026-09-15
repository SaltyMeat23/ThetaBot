"""Pure decision-metrics for the weekly brief: expected-move cushion, earnings-in-window,
assignment capacity, and open-position management flags. No I/O."""
import math
from dataclasses import dataclass
from datetime import date, timedelta

from agentic.services.brief_metrics import (
    assignment_capacity,
    earnings_before_expiry,
    expected_move,
    position_management,
    strike_cushion,
)


def test_expected_move_and_none_guards():
    # 100 * 0.40 * sqrt(10/365) ~= 6.62
    em = expected_move(100.0, 0.40, 10)
    assert em is not None and math.isclose(em, 100 * 0.40 * math.sqrt(10 / 365), rel_tol=1e-9)
    for bad in (expected_move(None, 0.4, 10), expected_move(100, 0, 10),
                expected_move(100, 0.4, 0), expected_move(-5, 0.4, 10)):
        assert bad is None


def test_strike_cushion_multiples():
    c = strike_cushion(price=100.0, strike=90.0, iv=0.40, dte=10, support=85.0)
    em = c["expected_move"]
    assert em and c["strike_em"] == round(10 / em, 2)                 # strike 10 below spot
    assert c["support_em"] == round(15 / em, 2)                       # support 15 below spot
    assert c["support_below_strike_em"] == round(5 / em, 2)           # support 5 below the strike


def test_strike_cushion_missing_inputs_are_none():
    c = strike_cushion(price=100.0, strike=90.0, iv=None, dte=10)
    assert c["expected_move"] is None and c["strike_em"] is None      # no IV -> no move, no cushion


def test_earnings_before_expiry():
    assert earnings_before_expiry(6, 10) is True                      # earnings inside the contract
    assert earnings_before_expiry(12, 10) is False                    # earnings after expiry
    assert earnings_before_expiry(None, 10) is None                   # unknown earnings date


def test_assignment_capacity():
    # two short puts: 90x1 + 50x2 -> (9000 + 10000) = 19000 collateral
    cap = assignment_capacity([(90.0, 1), (50.0, 2)], buying_power=12000.0)
    assert cap["collateral"] == 19000.0 and cap["buying_power"] == 12000.0
    assert cap["coverage"] == round(12000 / 19000, 2)
    assert cap["shortfall"] == 7000.0                                 # 19000 - 12000
    covered = assignment_capacity([(10.0, 1)], buying_power=5000.0)
    assert covered["shortfall"] == 0.0 and covered["coverage"] == 5.0


def test_assignment_capacity_no_collateral():
    cap = assignment_capacity([], buying_power=5000.0)
    assert cap["collateral"] == 0.0 and cap["coverage"] is None and cap["shortfall"] == 0.0


@dataclass
class _Pos:
    underlying: str
    option_type: str
    strike: float
    quantity: int
    expiration: date
    status: str = "OPEN"

    def dte(self, today=None):
        return (self.expiration - date.today()).days


def test_position_management_moneyness_and_roll_window():
    soon = date.today() + timedelta(days=5)
    far = date.today() + timedelta(days=40)
    puts = [
        _Pos("SMR", "put", 12.0, 1, soon),   # price 10 -> ITM (10 < 12), inside roll window
        _Pos("F", "put", 9.0, 1, far),       # price 13.8 -> OTM, far
        _Pos("TE", "put", 10.0, 1, far),     # price 10.4 -> near (within 1 ATR of 0.6)
        _Pos("X", "put", 5.0, 1, soon, status="CLOSED"),  # excluded (not open)
    ]
    rows = position_management(
        puts, price_by_symbol={"SMR": 10.0, "F": 13.8, "TE": 10.4},
        atr_by_symbol={"TE": 0.6})
    syms = [r["symbol"] for r in rows]
    assert "X" not in syms                                            # closed excluded
    assert syms[0] == "SMR"                                           # soonest DTE first
    by = {r["symbol"]: r for r in rows}
    assert by["SMR"]["moneyness"] == "ITM" and by["SMR"]["roll_window"] is True
    assert by["F"]["moneyness"] == "OTM" and by["F"]["roll_window"] is False
    assert by["TE"]["moneyness"] == "near"                            # 0.4 cushion <= 0.6 ATR


def test_position_management_without_price_still_reports_dte():
    far = date.today() + timedelta(days=30)
    rows = position_management([_Pos("AAA", "put", 20.0, 1, far)])
    assert rows[0]["moneyness"] is None and rows[0]["dte"] == 30 and rows[0]["roll_window"] is False
