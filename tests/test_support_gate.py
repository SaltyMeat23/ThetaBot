"""Strike-below-support gate: the scanner's _support_ceiling helper (fail-open semantics)."""
from types import SimpleNamespace

from agentic.config import EntryCriteria
from agentic.services.scanner import OpportunityScanner


class FakeTV:
    """Minimal TVIndicatorStore stand-in: returns a canned snapshot (or None) from get_latest."""
    def __init__(self, snap):
        self._snap = snap
        self.max_age_seen = None

    def get_latest(self, symbol, max_age_seconds=None):
        self.max_age_seen = max_age_seconds
        return self._snap


def _fake_self(tv, max_age=108000):
    return SimpleNamespace(tv_indicators=tv,
                           settings=SimpleNamespace(ai=SimpleNamespace(
                               tv_indicator_max_age_seconds=max_age)))


def _snap(**payload):
    return {"symbol": "X", "payload": payload}


ON = EntryCriteria(require_strike_below_support=True)
ON_BUF = EntryCriteria(require_strike_below_support=True, support_buffer_pct=0.02)
OFF = EntryCriteria(require_strike_below_support=False)


def _ceiling(fake, crit, sym="X"):
    return OpportunityScanner._support_ceiling(fake, sym, crit)


def test_gate_off_returns_none():
    assert _ceiling(_fake_self(FakeTV(_snap(support=95.0))), OFF) is None


def test_fresh_support_becomes_ceiling():
    assert _ceiling(_fake_self(FakeTV(_snap(support=95.0))), ON) == 95.0


def test_buffer_requires_margin_below_support():
    assert _ceiling(_fake_self(FakeTV(_snap(support=95.0))), ON_BUF) == 95.0 * 0.98


def test_gate_uses_configured_freshness_window():
    tv = FakeTV(_snap(support=95.0))
    _ceiling(_fake_self(tv, max_age=54321), ON)
    assert tv.max_age_seen == 54321


def test_missing_or_stale_snapshot_fails_open():
    assert _ceiling(_fake_self(FakeTV(None)), ON) is None            # no/stale snapshot
    assert _ceiling(_fake_self(None), ON) is None                   # no TV store at all


def test_invalid_support_values_fail_open():
    assert _ceiling(_fake_self(FakeTV(_snap())), ON) is None                 # key absent
    assert _ceiling(_fake_self(FakeTV(_snap(support=None))), ON) is None     # null
    assert _ceiling(_fake_self(FakeTV(_snap(support="19.1"))), ON) is None   # string, not number
    assert _ceiling(_fake_self(FakeTV(_snap(support=0.0))), ON) is None      # non-positive
    assert _ceiling(_fake_self(FakeTV(_snap(support=float("nan")))), ON) is None  # NaN
    assert _ceiling(_fake_self(FakeTV(_snap(support=True))), ON) is None     # bool is not a level
