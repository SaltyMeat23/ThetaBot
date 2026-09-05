"""CSP backtest engine — one position at a time, monthly cycles, real historical prices.

Reuses the LIVE screener (entry/screener.py) so it tests the actual strategy. Greeks are
reconstructed with Black-Scholes; slippage is a flat fraction of the option price (no historical
bid/ask exists). Conservative: assignment books its loss with no CC-recovery modeled.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..config import EntryCriteria
from ..marketdata.blackscholes import DEFAULT_RATE, bs_delta, implied_vol
from ..marketdata.quote import OptionContractQuote
from ..entry.screener import screen_candidates

log = logging.getLogger("agentic.backtest")

LIMITATIONS = (
    "Alpaca history is ~2 benign years (no crash); greeks are BS-computed (model error); "
    "slippage is estimated (no historical bid/ask); one position at a time; assignment booked "
    "as a loss with NO covered-call recovery modeled. Validates mechanics + relative tuning -- "
    "NOT proof of profitability or safety."
)


@dataclass
class BacktestReport:
    symbol: str
    start: str
    end: str
    trades: list[dict] = field(default_factory=list)
    n_trades: int = 0
    wins: int = 0
    win_rate: float | None = None
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    max_drawdown: float = 0.0
    assignment_rate: float | None = None
    avg_dte_held: float | None = None
    annualized_return: float | None = None
    limitations: str = LIMITATIONS


def _nearest_prior(sorted_dates: list[str], target: date) -> str | None:
    tgt, chosen = target.isoformat(), None
    for d in sorted_dates:
        if d <= tgt:
            chosen = d
        else:
            break
    return chosen


class CspBacktest:
    def __init__(
        self, market_data, criteria: EntryCriteria, *,
        profit_target_pct: float = 0.5, dte_close: int = 2, slippage_pct: float = 0.05,
        rate: float = DEFAULT_RATE,
    ):
        self.md = market_data
        self.criteria = criteria
        self.profit_target_pct = profit_target_pct
        self.dte_close = dte_close
        self.slip = slippage_pct
        self.rate = rate
        self.target_entry_dte = round((criteria.dte_min + criteria.dte_max) / 2)

    async def run(self, symbol: str, start: date, end: date) -> BacktestReport:
        lookback = (end - start).days + 90
        bars = await self.md.get_underlying_bars(symbol, lookback)
        closes = {b["date"]: b["c"] for b in bars if b.get("date") and b.get("c")}
        dates = sorted(closes)
        contracts = await self.md.list_option_contracts(
            symbol, start.isoformat(), (end + timedelta(days=60)).isoformat(), "put"
        )
        by_exp: dict[str, list[dict]] = {}
        for c in contracts:
            if c.get("expiration") and c.get("symbol") and c.get("strike"):
                by_exp.setdefault(c["expiration"], []).append(c)

        report = BacktestReport(symbol=symbol, start=start.isoformat(), end=end.isoformat())
        last_exit = start
        for exp_str in sorted(by_exp):
            try:
                exp = date.fromisoformat(exp_str)
            except ValueError:
                continue
            if exp <= start or exp > end + timedelta(days=45):
                continue
            entry_str = _nearest_prior(dates, exp - timedelta(days=self.target_entry_dte))
            if entry_str is None:
                continue
            entry_dt = date.fromisoformat(entry_str)
            if entry_dt < last_exit or entry_dt < start or entry_dt > end:
                continue  # still holding or out of window
            trade = await self._cycle(symbol, exp, entry_str, closes, dates, by_exp[exp_str])
            if trade is None:
                continue
            report.trades.append(trade)
            last_exit = date.fromisoformat(trade["exit_date"])
        return _finalize(report, start, end)

    async def _cycle(self, symbol, exp, entry_str, closes, dates, contracts) -> dict | None:
        entry_dt = date.fromisoformat(entry_str)
        spot = closes[entry_str]
        dte = (exp - entry_dt).days
        if dte <= self.dte_close:
            return None
        T = dte / 365.0

        # Build synthetic candidates for near-ATM put strikes, priced from real bars.
        quotes: list[OptionContractQuote] = []
        chosen_bars: dict[str, dict] = {}
        for c in contracts:
            k = c["strike"]
            if not (spot * 0.80 <= k <= spot * 1.03):
                continue
            obars = await self.md.get_option_bars(
                c["symbol"], (entry_dt - timedelta(days=4)).isoformat(), entry_str
            )
            price = _price_at(obars, entry_str)
            if price is None or price <= 0:
                continue
            iv = implied_vol(price, spot, k, T, self.rate, is_call=False)
            if iv is None:
                continue
            delta = bs_delta(spot, k, T, self.rate, iv, is_call=False)
            quotes.append(OptionContractQuote(
                occ_symbol=c["symbol"], underlying=symbol, option_id=None, option_type="put",
                strike=k, expiration=exp, bid=price, ask=price, mark=price, delta=delta, iv=iv,
                open_interest=None, volume=None,
            ))
            chosen_bars[c["symbol"]] = {"strike": k}

        ranked = screen_candidates(symbol, quotes, self.criteria, today=entry_dt)
        if not ranked:
            return None
        pick = ranked[0]
        credit = round(pick.premium * (1 - self.slip), 4)

        # Full life of the chosen contract for management.
        series_bars = await self.md.get_option_bars(pick.occ_symbol, entry_str, exp.isoformat())
        series = {b["date"]: b["c"] for b in series_bars if b.get("date") and b.get("c")}

        last_mark = pick.premium
        for d in dates:
            if d <= entry_str or d >= exp.isoformat():
                continue  # manage strictly before expiry; expiry handled below
            last_mark = series.get(d, last_mark)
            day = date.fromisoformat(d)
            if last_mark <= self.profit_target_pct * credit:  # profit target
                debit = round(last_mark * (1 + self.slip), 4)
                return _trade(pick, entry_str, d, credit, debit, "profit_target", (day - entry_dt).days)
            if (exp - day).days <= self.dte_close:  # DTE close
                debit = round(last_mark * (1 + self.slip), 4)
                return _trade(pick, entry_str, d, credit, debit, "dte_close", (day - entry_dt).days)

        # Expiry / assignment.
        exp_close = _nearest_prior(dates, exp)
        s_exp = closes.get(exp_close, spot) if exp_close else spot
        if s_exp >= pick.strike:  # OTM -> expire worthless (keep full credit)
            return _trade(pick, entry_str, (exp_close or entry_str), credit, 0.0, "expired", dte)
        # ITM -> assigned; loss beyond breakeven, no CC recovery modeled.
        debit = max(0.0, pick.strike - s_exp)
        return _trade(pick, entry_str, (exp_close or entry_str), credit, round(debit, 4),
                      "assigned", dte)


def _price_at(obars: list[dict], day: str) -> float | None:
    price = None
    for b in obars:
        if b.get("date") and b["date"] <= day and b.get("c"):
            price = b["c"]
    return price


def _trade(pick, entry_date, exit_date, credit, debit, reason, dte_held) -> dict:
    pnl = round((credit - debit) * 100, 2)
    return {
        "entry_date": entry_date, "exit_date": exit_date, "strike": pick.strike,
        "expiration": pick.expiration.isoformat(), "delta": pick.delta, "iv": pick.iv,
        "credit": credit, "close_debit": round(debit, 4), "exit_reason": reason,
        "dte_held": dte_held, "pnl": pnl, "outcome": "win" if pnl > 0 else "loss",
    }


def _finalize(r: BacktestReport, start: date, end: date) -> BacktestReport:
    t = r.trades
    r.n_trades = len(t)
    r.wins = sum(1 for x in t if x["pnl"] > 0)
    r.total_pnl = round(sum(x["pnl"] for x in t), 2)
    r.win_rate = round(r.wins / r.n_trades, 3) if r.n_trades else None
    r.avg_pnl = round(r.total_pnl / r.n_trades, 2) if r.n_trades else 0.0
    assigned = sum(1 for x in t if x["exit_reason"] == "assigned")
    r.assignment_rate = round(assigned / r.n_trades, 3) if r.n_trades else None
    r.avg_dte_held = round(sum(x["dte_held"] for x in t) / r.n_trades, 1) if r.n_trades else None
    # max drawdown of the cumulative-P&L equity curve
    cum, peak, dd = 0.0, 0.0, 0.0
    for x in t:
        cum += x["pnl"]
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    r.max_drawdown = round(dd, 2)
    avg_collateral = (sum(x["strike"] for x in t) / len(t) * 100) if t else 0.0
    years = max((end - start).days / 365.0, 1e-9)
    if avg_collateral > 0:
        r.annualized_return = round((r.total_pnl / avg_collateral) / years, 4)
    return r
