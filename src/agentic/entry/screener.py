"""CSP candidate screener — pure, side-effect-free (mirrors services/stats.py).

Ports the ThetaDaddies screener's criteria, math, and liquidity-hygiene rules to Python, and
applies the research-backed CSP entry filters from EntryCriteria. Input is one underlying's
option chain (list[OptionContractQuote]); output is ranked EntryCandidate[].

Math (from ThetaDaddies types/screener.ts):
  premium      = mid (bid/ask midpoint)
  ror%         = premium / strike * 100
  annualized%  = ror% / dte * 365
  max_risk$    = strike * 100   (cash secured per contract)
  break_even   = strike - premium

Liquidity hygiene (ThetaDaddies): drop spread > max_spread_pct of mid; drop penny-no-bid;
drop zero-volume-AND-zero-OI. Open interest / volume are only enforced when the feed provides
them (Alpaca snapshots may omit OI) — unknown values are not treated as failures.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ..config import EntryCriteria
from ..domain.models import utcnow
from ..marketdata.quote import OptionContractQuote
from ..services.brief_metrics import expected_move


@dataclass
class EntryCandidate:
    underlying: str
    occ_symbol: str
    option_id: str | None
    strike: float
    expiration: date
    dte: int
    delta: float | None
    iv: float | None
    premium: float
    ror: float            # return on risk, percent
    annualized_ror: float  # percent
    max_risk: float       # dollars of collateral per contract
    break_even: float
    open_interest: int | None
    volume: int | None
    score: float          # legacy ranking key = annualized_ror (kept for display/back-compat)
    theta: float | None = None          # per-share daily decay we collect (short premium)
    gamma: float | None = None          # assignment-risk acceleration
    theta_efficiency: float = 0.0        # daily decay per $ of collateral — the ranking key


def passes_candidate_gates(cand: "EntryCandidate", ctx, crit: EntryCriteria) -> str | None:
    """Context-aware per-candidate gates that need the underlying's vol profile, so they can't live
    in screen_candidates (which sees only the chain). Both opt-in; fail-open on missing data. Returns
    None on pass (or when a gate's data is unavailable), else a short reason.
      * min_iv_rv_ratio          — variance-risk-premium floor: sold IV vs the name's realized vol.
      * min_strike_expected_moves — require the strike to sit >= N option-implied expected-moves OUT
        of the money, scaling the cushion to the name's own volatility (the BULL lesson).
    """
    rv = getattr(ctx, "realized_vol", None)
    price = getattr(ctx, "price", None)
    if crit.min_iv_rv_ratio is not None and cand.iv is not None and rv:
        ratio = cand.iv / rv
        if ratio < crit.min_iv_rv_ratio:
            return f"iv/rv {ratio:.2f} < {crit.min_iv_rv_ratio} (thin variance premium)"
    if crit.min_strike_expected_moves is not None:
        em = expected_move(price, cand.iv, cand.dte)
        if em and price is not None:
            strike_em = (price - cand.strike) / em
            if strike_em < crit.min_strike_expected_moves:
                return f"cushion {strike_em:.2f}x EM < {crit.min_strike_expected_moves}x (thin)"
    return None


def _passes_liquidity(c: OptionContractQuote, crit: EntryCriteria) -> bool:
    # penny-no-bid
    if c.bid is None or c.bid <= 0:
        return False
    # spread must be computable and within cap
    spread = c.spread_pct
    if spread is None or spread > crit.max_spread_pct:
        return False
    # zero-volume AND zero-OI (only when both are known)
    if c.volume == 0 and c.open_interest == 0:
        return False
    # floors, enforced only when the data is present
    if c.open_interest is not None and c.open_interest < crit.min_open_interest:
        return False
    if c.volume is not None and c.volume < crit.min_volume:
        return False
    return True


def screen_candidates(
    underlying: str,
    chain: list[OptionContractQuote],
    criteria: EntryCriteria,
    today: date | None = None,
    *,
    option_type: str = "put",
    strike_floor: float | None = None,
    support_ceiling: float | None = None,
    strike_ceiling: float | None = None,
    ignore_delta: bool = False,
) -> list[EntryCandidate]:
    """Screen one underlying's chain, ranked by yield.

    option_type="put" -> cash-secured puts (default). option_type="call" -> covered calls;
    pass strike_floor=cost_basis so we never sell a call below what the shares cost (which
    would risk being called away at a loss). RoR math is the same for both.

    support_ceiling (puts): when set, drop any strike ABOVE it, so a TradingView support level
    can act as a cushion — the stock must break support before our strike is threatened.
    """
    today = today or utcnow().date()
    out: list[EntryCandidate] = []
    for c in chain:
        if c.option_type != option_type:
            continue
        if strike_floor is not None and c.strike < strike_floor:
            continue
        if support_ceiling is not None and c.strike > support_ceiling:
            continue
        if strike_ceiling is not None and c.strike > strike_ceiling:
            continue
        dte = c.dte(today)
        if dte < criteria.dte_min or dte > criteria.dte_max:
            continue
        if c.delta is None and not ignore_delta:
            continue
        adelta = abs(c.delta) if c.delta is not None else 0.0
        if not ignore_delta and (adelta < criteria.delta_min or adelta > criteria.delta_max):
            continue
        premium = c.midpoint
        if premium is None or premium <= 0 or c.strike <= 0:
            continue
        if not _passes_liquidity(c, criteria):
            continue
        ror = premium / c.strike * 100.0
        annualized = (ror / dte * 365.0) if dte > 0 else 0.0
        if annualized < criteria.min_annualized_yield * 100.0:
            continue
        # Theta-efficiency = daily premium decay per $ of collateral. Uses the real theta from the
        # OPRA feed when present (the actual decay rate right now), else the average-decay proxy
        # (premium/strike/dte). This ranks by "best premium to actually harvest", not just yield.
        theta_eff = (abs(c.theta) / c.strike) if c.theta is not None else (
            premium / c.strike / dte if dte > 0 else 0.0)
        # NOTE: earnings blackout (criteria.exclude_earnings_days) needs an earnings calendar
        # feed we don't yet ingest — not enforced in v1 (tracked as a follow-up).
        out.append(EntryCandidate(
            underlying=underlying,
            occ_symbol=c.occ_symbol,
            option_id=c.option_id,
            strike=c.strike,
            expiration=c.expiration,
            dte=dte,
            delta=c.delta,
            iv=c.iv,
            premium=round(premium, 4),
            ror=round(ror, 4),
            annualized_ror=round(annualized, 4),
            max_risk=round(c.strike * 100.0, 2),
            break_even=round(c.strike - premium, 4),
            open_interest=c.open_interest,
            volume=c.volume,
            score=round(annualized, 4),
            theta=c.theta,
            gamma=c.gamma,
            theta_efficiency=round(theta_eff, 6),
        ))
    out.sort(key=lambda x: x.theta_efficiency, reverse=True)  # rank by decay-per-collateral
    return out
