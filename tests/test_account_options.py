"""Advisory account-options service: covered calls above cost basis, CSPs sized to buying power."""
from datetime import timedelta

import pytest

from agentic.config import EntryCriteria, Settings
from agentic.domain.models import EquityHolding, utcnow
from agentic.entry.screener import EntryCandidate
from agentic.marketdata.quote import OptionContractQuote
from agentic.services.account_options import account_option_suggestions

EXP = utcnow().date() + timedelta(days=10)


def _call(strike, delta, bid, ask):
    return OptionContractQuote(
        occ_symbol=f"X{strike}c", underlying="X", option_id=None, option_type="call",
        strike=strike, expiration=EXP, bid=bid, ask=ask, mark=round((bid + ask) / 2, 4),
        delta=delta, iv=0.5, open_interest=500, volume=50)


class _MD:
    def __init__(self, chains):
        self._chains = chains

    async def get_chain(self, sym):
        return self._chains.get(sym, [])


class _Broker:
    def __init__(self, holdings, bp=0.0, av=100_000.0, opens=None):
        self._h, self._bp, self._av, self._o = holdings, bp, av, opens or []

    async def get_equity_positions(self, account_number=None):
        return self._h

    async def get_buying_power(self, account_number=None):
        return self._bp

    async def get_account_value(self, account_number=None):
        return self._av

    async def get_open_positions(self, account_number=None):
        return self._o


_CC_CRIT = {"dte_min": 5, "dte_max": 20, "delta_min": 0.15, "delta_max": 0.35,
            "min_annualized_yield": 0.05, "min_open_interest": 1, "min_volume": 1,
            "max_spread_pct": 0.25}


@pytest.mark.asyncio
async def test_covered_calls_flag_below_basis_and_sized_to_shares():
    # Advisory: no cost-basis floor — both strikes surface, and the below-basis one is flagged so
    # the user can knowingly sell it to lower cost basis (esp. in a non-taxable account).
    md = _MD({"X": [_call(48, 0.28, 1.5, 1.6), _call(55, 0.25, 1.4, 1.5)]})  # 48<basis, 55>basis
    broker = _Broker([EquityHolding(symbol="X", quantity=200, average_cost=50.0)])
    settings = Settings(entry={"cc_criteria": _CC_CRIT, "watchlist": []})
    res = await account_option_suggestions(broker, md, settings, "ACCT", csp_candidates=[])
    by_strike = {c["strike"]: c for c in res["covered_calls"]}
    assert 48 in by_strike and 55 in by_strike           # both shown (advisory, no floor)
    assert by_strike[48]["below_basis"] is True          # 48 < 50 cost basis -> flagged
    assert by_strike[55]["below_basis"] is False
    assert all(c["contracts"] == 2 for c in res["covered_calls"])   # 200 shares -> 2 coverable
    assert res["holdings"][0]["coverable"] == 2
    assert res["covered_calls"][0]["weekly_yield_pct"] > 0


@pytest.mark.asyncio
async def test_covered_calls_skipped_when_all_shares_written():
    from agentic.domain.enums import Direction, OptionType, Strategy
    from agentic.domain.models import Position
    md = _MD({"X": [_call(55, 0.25, 1.4, 1.5)]})
    short_call = Position(occ_symbol="X55c", underlying="X", option_type=OptionType.CALL,
                          strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=2,
                          strike=55, expiration=EXP, credit_received=1.4)
    broker = _Broker([EquityHolding(symbol="X", quantity=200, average_cost=50.0)], opens=[short_call])
    settings = Settings(entry={"cc_criteria": _CC_CRIT, "watchlist": []})
    res = await account_option_suggestions(broker, md, settings, "A", csp_candidates=[])
    assert res["covered_calls"] == []                   # 2 shares' worth already written
    assert res["holdings"][0]["coverable"] == 0


@pytest.mark.asyncio
async def test_csp_sized_to_buying_power():
    cand = EntryCandidate(
        underlying="Z", occ_symbol="Z10p", option_id=None, strike=10.0, expiration=EXP, dte=10,
        delta=-0.25, iv=0.5, premium=0.30, ror=3.0, annualized_ror=100.0, max_risk=1000,
        break_even=9.7, open_interest=500, volume=50, score=100.0)
    broker = _Broker([], bp=5000.0, av=5000.0)
    res = await account_option_suggestions(broker, _MD({}), Settings(), "A", csp_candidates=[cand])
    assert len(res["cash_secured_puts"]) == 1
    assert res["cash_secured_puts"][0]["contracts"] == 5      # 5000 / (10*100)
    assert res["cash_secured_puts"][0]["collateral"] == 5000.0
