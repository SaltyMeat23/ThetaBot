"""Broker-fallback safety net: detect + surface the live-but-on-paper degraded state."""
from types import SimpleNamespace

from agentic.brokers.base import BrokerCapabilities
from agentic.brokers.factory import broker_degraded
from agentic.config import Settings
from agentic.services.reporting import build_daily_digest


def _broker(name, is_paper):
    return SimpleNamespace(
        capabilities=lambda: BrokerCapabilities(name, supports_options_orders=True,
                                                is_paper=is_paper))


def test_broker_degraded_only_when_live_and_fallen_to_paper():
    live_rh = Settings(mode="live", i_understand_live_trading=True,
                       broker="robinhood_mcp", market_data="paper")
    paper_cfg = Settings(mode="paper", broker="paper", market_data="paper")

    # Live-armed, configured for RH, but running on paper -> DEGRADED.
    assert broker_degraded(live_rh, _broker("paper", True)) is True
    # Live-armed and actually on RH -> fine.
    assert broker_degraded(live_rh, _broker("robinhood_mcp", False)) is False
    # Intentionally paper (mode=paper) -> not degraded, even on the paper broker.
    assert broker_degraded(paper_cfg, _broker("paper", True)) is False
    # Live but configured broker IS paper -> not a fallback, not degraded.
    live_paper = Settings(mode="live", i_understand_live_trading=True,
                          broker="paper", market_data="paper")
    assert broker_degraded(live_paper, _broker("paper", True)) is False


def test_digest_flags_degraded_state():
    _title, msg = build_daily_digest(stats={}, rows=[], scanner=None, mode="live", degraded=True)
    assert "ON SIMULATOR" in msg and "real account" in msg
    # No false alarm when healthy.
    _t2, ok = build_daily_digest(stats={}, rows=[], scanner=None, mode="live", degraded=False)
    assert "ON SIMULATOR" not in ok
