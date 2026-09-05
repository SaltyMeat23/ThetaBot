"""Phase 3: SignalMatcher — occ exact, underlying single, underlying fan-out forces approval."""
from datetime import date, timedelta

from agentic.domain.enums import Direction, OptionType, Strategy
from agentic.domain.models import Position, Signal
from agentic.rules.signal_rule import SignalMatcher


def _pos(occ: str, underlying: str) -> Position:
    return Position(
        occ_symbol=occ, underlying=underlying, option_type=OptionType.CALL,
        strategy=Strategy.COVERED_CALL, direction=Direction.SHORT, quantity=1,
        strike=100.0, expiration=date.today() + timedelta(days=20), credit_received=1.0,
    )


def _sig(**raw) -> Signal:
    return Signal(raw=raw, dedup_key="k")


def test_occ_exact_match():
    positions = [_pos("AAPL250101C00100000", "AAPL"), _pos("MSFT250101C00400000", "MSFT")]
    m = SignalMatcher(base_requires_approval=False)
    matches = m.match(_sig(occ="AAPL250101C00100000", action="close"), positions)
    assert len(matches) == 1
    assert matches[0].position.underlying == "AAPL"
    assert matches[0].requires_approval is False  # exact occ honors config


def test_underlying_single_match():
    positions = [_pos("AAPL250101C00100000", "AAPL")]
    m = SignalMatcher(base_requires_approval=True)
    matches = m.match(_sig(symbol="AAPL"), positions)
    assert len(matches) == 1
    assert matches[0].requires_approval is True


def test_underlying_fanout_forces_approval():
    positions = [_pos("AAPL250101C00100000", "AAPL"), _pos("AAPL250201C00110000", "AAPL")]
    m = SignalMatcher(base_requires_approval=False)  # config says auto...
    matches = m.match(_sig(symbol="AAPL"), positions)
    assert len(matches) == 2
    # ...but fan-out overrides to require approval for safety.
    assert all(mt.requires_approval is True for mt in matches)


def test_no_match():
    positions = [_pos("AAPL250101C00100000", "AAPL")]
    m = SignalMatcher()
    assert m.match(_sig(symbol="TSLA"), positions) == []
    assert m.match(_sig(), positions) == []
