"""Black-Scholes pricing/IV inversion + the historical IV backfill orchestration."""
import math
from datetime import date

import pytest

from agentic.marketdata.blackscholes import bs_price, implied_vol
from agentic.store.db import Database
from agentic.store.trade_journal import TradeJournalStore
from agentic.tools.backfill_iv import IvBackfill

R = 0.045


def test_put_call_parity():
    S, K, T, sig = 100.0, 100.0, 0.5, 0.3
    call = bs_price(S, K, T, R, sig, is_call=True)
    put = bs_price(S, K, T, R, sig, is_call=False)
    # call - put == S - K*e^{-rT}
    assert math.isclose(call - put, S - K * math.exp(-R * T), abs_tol=1e-6)


def test_implied_vol_roundtrip():
    for sig in (0.15, 0.35, 0.8):
        price = bs_price(100.0, 100.0, 0.5, R, sig, is_call=False)
        recovered = implied_vol(price, 100.0, 100.0, 0.5, R, is_call=False)
        assert recovered is not None and abs(recovered - sig) < 0.01


def test_implied_vol_below_intrinsic_returns_none():
    # A put price below intrinsic (K-S) can't be inverted.
    assert implied_vol(0.5, 90.0, 100.0, 0.5, R, is_call=False) is None


class _StubMD:
    def __init__(self, closes, contracts, option_bar_price, entry_str):
        self._closes = closes
        self._contracts = contracts
        self._price = option_bar_price
        self._entry = entry_str

    async def get_underlying_bars(self, symbol, lookback_days=730):
        return [{"date": d, "c": c} for d, c in sorted(self._closes.items())]

    async def list_option_contracts(self, symbol, exp_gte, exp_lte, opt_type="put"):
        return self._contracts

    async def get_option_bars(self, symbol, start, end):
        return [{"date": self._entry, "c": self._price}]


@pytest.mark.asyncio
async def test_backfill_records_iv(tmp_path):
    journal = TradeJournalStore(Database(tmp_path / "iv.db"))
    exp = date(2026, 3, 20)
    entry = date(2026, 2, 13)          # exp - 35 days
    # daily closes across the window, spot = 10
    closes = {}
    d = date(2026, 1, 1)
    while d <= date(2026, 3, 19):
        closes[d.isoformat()] = 10.0
        d = date.fromordinal(d.toordinal() + 1)
    contracts = [
        {"symbol": "X..P9", "strike": 9.0, "expiration": exp.isoformat(), "type": "put"},
        {"symbol": "X..P10", "strike": 10.0, "expiration": exp.isoformat(), "type": "put"},
        {"symbol": "X..P11", "strike": 11.0, "expiration": exp.isoformat(), "type": "put"},
    ]
    true_sigma = 0.40
    price = bs_price(10.0, 10.0, (exp - entry).days / 365.0, R, true_sigma, is_call=False)
    md = _StubMD(closes, contracts, price, entry.isoformat())

    recorded = await IvBackfill(md, journal, rate=R).backfill_symbol("X")
    assert recorded == 1
    hist = dict(journal.iv_history("X"))
    assert entry.isoformat() in hist
    assert abs(hist[entry.isoformat()] - true_sigma) < 0.02   # recovered the ATM IV
