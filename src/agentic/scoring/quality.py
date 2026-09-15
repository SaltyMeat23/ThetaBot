"""Quality/growth score — a transparent, evidence-graded tilt for the entry scanner.

Blends the *measurable* traits that long-run winners share into a single 0-100 number:

  * **Profitability / quality** — gross profitability ((rev-COGS)/assets) or margins. Novy-Marx
    (2013): the profitability factor predicts returns about as well as value; it's in the
    Fama-French 5-factor model. STRONG evidence.
  * **Cash generation** — FCF margin. The single most consistent trait of durable compounders.
    STRONG (practitioner + multibagger studies).
  * **Growth durability** — YoY revenue growth, rewarding a durable ~10-30% band rather than
    unsustainable hypergrowth. MODERATE.
  * **Momentum / trend confirmation** — 12-1 month price momentum, haircut if below the 200-SMA.
    STRONG *as confirmation*, not prediction — don't sell puts into a broken downtrend.
  * **Insider open-market buys** (Phase B, EDGAR Form 4) — a small upward-only bonus; executives
    buying their own stock is a documented positive signal, while absence is neutral, not bearish.

A refined junk floor caps genuine junk (cash-burning AND unprofitable AND not funding real growth)
low, but spares high-growth or high-margin reinvestors — the speculative growers this is built to
find. Cash-burn (FCF margin) and gross profitability come from EDGAR (Phase B); with only RH data
(Phase A) the cash lens is absent and the score leans on margins + growth + momentum.

Honest by design: this is NOT a multibagger predictor (winners are rare and unpredictable ex-ante —
Bessembinder). It only *tilts toward* winner-traits and *screens out* junk. Every sub-score is
optional; the blend renormalizes over whatever data is present, and returns None when nothing is
available (fail-open — a name with no data is never penalized, just un-tilted). Pure and
side-effect-free so the analytics flywheel can later tell us whether the score actually predicted
better outcomes on our OWN trades — the only source of truth we fully trust.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..marketdata.company_data import CompanyProfile

# Blend weights over the sub-scores that are present (renormalized if some are missing).
_WEIGHTS = {"profitability": 0.30, "cash": 0.25, "growth": 0.25, "momentum": 0.20}


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def _lin(x: float, lo: float, hi: float) -> float:
    """Linear map x in [lo, hi] -> [0, 100], clamped outside the band."""
    if hi <= lo:
        return 50.0
    return _clamp((x - lo) / (hi - lo) * 100.0)


def _profitability_subscore(p: "CompanyProfile") -> float | None:
    """Blend two quality lenses when both exist: Novy-Marx gross profitability (capital efficiency,
    from EDGAR) and the best available margin (product quality, from RH). Averaging them keeps a
    high-margin but capital-heavy grower (e.g. a data-center build-out) from being scored purely on
    either lens alone."""
    parts: list[float] = []
    if p.gross_profitability is not None:      # Novy-Marx GP/A: ~0 poor, ~0.33 avg, 0.5+ strong
        parts.append(_lin(p.gross_profitability, -0.05, 0.50))
    if p.operating_margin is not None:         # <=0 unprofitable, 30%+ excellent
        parts.append(_lin(p.operating_margin, -0.10, 0.30))
    elif p.gross_margin is not None:           # 15% (retail) .. 70% (software)
        parts.append(_lin(p.gross_margin, 0.15, 0.70))
    elif p.net_margin is not None:
        parts.append(_lin(p.net_margin, -0.10, 0.25))
    if not parts:
        return None
    return round(sum(parts) / len(parts), 1)


def _insider_bonus(p: "CompanyProfile | None") -> float:
    """Small upward nudge for insider open-market BUYS in the last 90 days (a documented signal).
    Absence is neutral, not bearish (insiders sell for many reasons) — so this only ever adds."""
    n = getattr(p, "insider_net_buys_90d", None) if p else None
    if not n or n <= 0:
        return 0.0
    return min(10.0, 3.0 + 3.0 * n)  # 1 buyer -> +6, 2 -> +9, 3+ -> +10


def _cash_subscore(p: "CompanyProfile") -> float | None:
    if p.fcf_margin is None:
        return None
    return round(_lin(p.fcf_margin, -0.10, 0.25), 1)


def _growth_subscore(p: "CompanyProfile") -> float | None:
    g = p.revenue_growth
    if g is None:
        return None
    if g <= -0.05:                             # shrinking
        return round(_clamp(10 + (g + 0.05) * 100), 1)  # steeper decline -> toward 0
    if g < 0.10:                               # 0-10%: modest
        return round(10 + (g + 0.05) / 0.15 * 45, 1)    # -5%->10 .. 10%->55
    if g <= 0.30:                              # 10-30%: durable sweet spot
        return round(55 + (g - 0.10) / 0.20 * 45, 1)    # 10%->55 .. 30%->100
    return 90.0                                # >30%: strong, slight haircut (sustainability risk)


def _momentum_subscore(bars: list[dict] | None, below_sma200: bool | None = None) -> float | None:
    closes = [b["c"] for b in (bars or []) if isinstance(b, dict) and b.get("c") is not None]
    if len(closes) < 2:
        return None
    # 12-1 momentum (skip the most recent ~month to avoid short-term reversal); fall back to the
    # full available window when there isn't a year of history.
    if len(closes) >= 253:
        past, recent = closes[-253], closes[-22]
    else:
        past, recent = closes[0], closes[-1]
    if past <= 0:
        return None
    mom = recent / past - 1.0
    if mom <= -0.25:
        base = _clamp(10 + (mom + 0.25) * 100)   # deeper decline -> toward 0
    elif mom < 0.0:
        base = 10 + (mom + 0.25) / 0.25 * 40      # -25%->10 .. 0%->50
    elif mom <= 0.40:
        base = 50 + mom / 0.40 * 40               # 0%->50 .. 40%->90
    else:
        base = 90.0                               # cap: don't over-reward a parabola
    if below_sma200 is True:                      # broken trend -> haircut
        base *= 0.8
    return round(_clamp(base), 1)


def quality_breakdown(profile: "CompanyProfile | None", bars: list[dict] | None = None,
                      below_sma200: bool | None = None) -> dict:
    """Per-factor sub-scores + the blended score (for logging/debugging/tests)."""
    subs: dict[str, float | None] = {
        "profitability": _profitability_subscore(profile) if profile else None,
        "cash": _cash_subscore(profile) if profile else None,
        "growth": _growth_subscore(profile) if profile else None,
        "momentum": _momentum_subscore(bars, below_sma200),
    }
    present = {k: v for k, v in subs.items() if v is not None}
    score: float | None = None
    if present:
        wsum = sum(_WEIGHTS[k] for k in present)
        blended = sum(_WEIGHTS[k] * v for k, v in present.items()) / wsum
        # Momentum alone is "unknown quality", not "high quality": if we have NO fundamental read
        # (no profitability/cash/growth sub-score), cap at neutral so a hot chart on a name we know
        # nothing about can't masquerade as high quality. Momentum can still pull such a name DOWN.
        has_fundamental = any(present.get(k) is not None for k in ("profitability", "cash", "growth"))
        if not has_fundamental:
            blended = min(blended, 50.0)
        # Refined junk floor: cap low ONLY for genuine junk — cash-burning AND unprofitable AND
        # not clearly funding real expansion. A high-growth OR high-gross-margin reinvestor (the
        # speculative growers we WANT) is spared; a stagnant low-margin cash-burner is not.
        if profile is not None:
            burning = profile.fcf_margin is not None and profile.fcf_margin < 0
            unprofitable = ((profile.operating_margin is not None and profile.operating_margin < 0)
                            or (profile.net_margin is not None and profile.net_margin < 0))
            reinvesting = ((profile.revenue_growth is not None and profile.revenue_growth >= 0.15)
                           or (profile.gross_margin is not None and profile.gross_margin >= 0.40))
            if burning and unprofitable and not reinvesting:
                blended = min(blended, 30.0)
        score = round(_clamp(blended + _insider_bonus(profile)), 1)
    subs["insider_bonus"] = round(_insider_bonus(profile), 1) if profile else 0.0
    subs["score"] = score
    return subs


def quality_growth_score(profile: "CompanyProfile | None", bars: list[dict] | None = None,
                         below_sma200: bool | None = None) -> float | None:
    """Blended 0-100 quality/growth score, or None when no sub-score is computable (fail-open)."""
    return quality_breakdown(profile, bars, below_sma200)["score"]
