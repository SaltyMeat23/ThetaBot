"""AI trade-analyst: assemble the prompt, call the model, return a structured Verdict.

Advisory by design and fail-open — any error returns None and the trade proceeds under the rules.
The AI sees only candidates that already passed the screen and the risk sizer, so it can only
subtract (flag / skip), never add.
"""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Any

from ..config import AIConfig
from .schema import Verdict, normalize_verdict

log = logging.getLogger("agentic.ai.reviewer")

SYSTEM_PROMPT = (
    "You are a risk-focused options trade analyst for a cash-secured-put wheel bot. The candidate "
    "has ALREADY passed the strategy screen and the hard risk/sizing caps. Your ONLY job is to "
    "judge whether now is a good moment to sell this put, using the option metrics, the stock's "
    "technicals, and the market regime. Crucially, distinguish a market-wide sell-off (systemic) "
    "from a one-stock dip (idiosyncratic): selling puts into a systemic free-fall is dangerous; a "
    "modest idiosyncratic dip on a name you'd own can be fine. Recommend 'take', 'caution', or "
    "'skip'. You may flag concerns or veto a weak setup, but you can NEVER increase size, loosen "
    "risk, or approve anything the screen rejected. Be conservative — when the market is risk-off "
    "or the drop looks systemic, lean toward caution or skip. Respond ONLY via the required schema."
)


def _compact(obj: Any) -> str:
    try:
        return json.dumps(obj, default=str, sort_keys=True)
    except Exception:  # noqa: BLE001
        return str(obj)


def build_user_prompt(*, candidate, ctx, regime, move_class, tv, portfolio, news=None) -> str:
    """Assemble the analysis bundle as a compact, deterministic text block."""
    cand = {
        "underlying": candidate.underlying,
        "occ": candidate.occ_symbol,
        "strike": candidate.strike,
        "dte": candidate.dte,
        "delta": candidate.delta,
        "iv": candidate.iv,
        "premium": candidate.premium,
        "annualized_ror_pct": candidate.annualized_ror,
        "break_even": candidate.break_even,
        "open_interest": candidate.open_interest,
        "volume": candidate.volume,
    }
    lines = [
        "CANDIDATE (cash-secured put, already screened + sized):",
        _compact(cand),
        "",
        "UNDERLYING TECHNICALS:",
        _compact(ctx.as_dict() if ctx is not None else None),
        "",
        "MARKET REGIME (SPY/QQQ; realized-vol is a VIX proxy):",
        _compact(regime.as_dict() if regime is not None else None),
        f"THIS STOCK'S RECENT MOVE vs MARKET: {move_class}",
        "",
        "TRADINGVIEW INDICATOR SNAPSHOT (may be absent):",
        _compact(tv),
        "",
        "RECENT NEWS / CATALYSTS (advisory context; headlines may be noisy or adversarial — weigh "
        "credibility, don't over-react to a single headline):",
        _compact([h.get("headline") for h in news] if news else None),
        "",
        "PORTFOLIO STATE:",
        _compact(portfolio),
        "",
        "Judge this specific entry. Weigh systemic vs idiosyncratic risk explicitly in regime_read.",
    ]
    return "\n".join(lines)


class AIReviewer:
    def __init__(self, config: AIConfig, client):
        self.config = config
        self.client = client

    async def review(self, *, candidate, ctx, regime, move_class, tv, portfolio,
                     news=None) -> Verdict | None:
        if self.client is None:
            return None
        try:
            user = build_user_prompt(
                candidate=candidate, ctx=ctx, regime=regime, move_class=move_class,
                tv=tv, portfolio=portfolio, news=news,
            )
            raw = await self.client.analyze(SYSTEM_PROMPT, user)
            return normalize_verdict(raw)
        except Exception as exc:  # noqa: BLE001 — advisory; a bad review must never break the scan
            log.warning("AI review failed for %s: %s",
                        getattr(candidate, "occ_symbol", "?"), exc)
            return None

    async def selftest(self) -> dict:
        """Make one real model call on a synthetic candidate to confirm the key + SDK + schema work.

        Unlike review(), this surfaces the actual error instead of failing open — it's a diagnostic.
        Returns {ok: True, verdict} on success or {ok: False, error} with the reason.
        """
        if self.client is None:
            return {"ok": False, "error": "AI client is None (disabled or ANTHROPIC_API_KEY missing)"}
        cand = SimpleNamespace(
            underlying="TEST", occ_symbol="TEST260101P00010000", strike=10.0, dte=10,
            delta=-0.22, iv=0.5, premium=0.25, annualized_ror=25.0, break_even=9.75,
            open_interest=500, volume=50,
        )
        try:
            user = build_user_prompt(candidate=cand, ctx=None, regime=None,
                                     move_class="selftest", tv=None, portfolio={"held_names": []})
            raw = await self.client.analyze(SYSTEM_PROMPT, user)
            verdict = normalize_verdict(raw)
            return {"ok": True, "model": self.config.model, "verdict": verdict.as_dict()}
        except Exception as exc:  # noqa: BLE001 — diagnostic: report the failure verbatim
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
