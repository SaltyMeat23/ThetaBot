"""Signal -> position matching for TradingView close alerts (Phase 3).

Unlike PROFIT_TARGET/DTE (evaluated per position each cycle), a SIGNAL is evaluated
against the set of open positions when a webhook arrives:

  * If the signal carries an exact ``occ`` symbol that matches an open position -> that one.
  * Otherwise match by ``underlying`` -> every open position on that underlying. If more
    than one matches (fan-out), approval is FORCED regardless of config, because the
    intent is ambiguous (which leg/expiry did the alert mean?).

Returns ``SignalMatch`` rows; the signal processor turns each into a CloseDecision.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..domain.models import Position, Signal


@dataclass
class SignalMatch:
    position: Position
    requires_approval: bool
    reason: str


class SignalMatcher:
    def __init__(self, *, base_requires_approval: bool = True):
        # Signals default to approval-gated; config can lower it for exact-occ matches only.
        self.base_requires_approval = base_requires_approval

    def match(self, signal: Signal, open_positions: list[Position]) -> list[SignalMatch]:
        occ = (signal.raw.get("occ") or signal.raw.get("occ_symbol") or "").strip()
        symbol = (signal.raw.get("symbol") or signal.raw.get("underlying") or "").strip().upper()

        if occ:
            exact = [p for p in open_positions if p.occ_symbol == occ]
            if exact:
                return [
                    SignalMatch(p, self.base_requires_approval,
                               f"TradingView signal matched contract {occ}")
                    for p in exact
                ]

        if symbol:
            by_underlying = [p for p in open_positions if p.underlying.upper() == symbol]
            fan_out = len(by_underlying) > 1
            return [
                SignalMatch(
                    p,
                    True if fan_out else self.base_requires_approval,
                    f"TradingView signal on {symbol}"
                    + (f" (fan-out across {len(by_underlying)} positions — approval forced)"
                       if fan_out else ""),
                )
                for p in by_underlying
            ]

        return []
