"""Pine exporter contract guard.

Asserts the field names the versioned Pine alerts emit still cover exactly what the bot's scanner
consumes. If an exporter renames a key (e.g. bb_percent_b -> percent_b) or the scanner's expected
key set changes without the other, this fails — killing the silent fail-open coupling.

Two exporters are guarded: the Daily "Feature Exporter" (gated technicals + daily setup flags) and
the intraday "Setup Exporter" (i_-prefixed real-time flags). Skips when pine/ is absent:
scripts/sync_to_thetabot.sh copies only src/+tests/, so the public ThetaBot repo won't have pine/.
"""
import re
from pathlib import Path

import pytest

from agentic.tools.tv_reconcile import EXPECTED_KEYS, SCANNER_CONSUMED, SETUP_CONSUMED

PINE_DIR = Path(__file__).resolve().parents[1] / "pine"
PINE = PINE_DIR / "feature_exporter.pine"
SETUP_PINE = PINE_DIR / "setup_exporter.pine"

# Of the scanner-consumed keys, `support` (and `resistance`) are emitted by the SEPARATE 30m
# "Support Resistance Channels" script, not this Daily exporter. So this file only needs to emit the
# technical gate fields it owns. See pine/feature_exporter.contract.md ("two alerts merge").
EXPORTER_OWNS = SCANNER_CONSUMED - {"support", "resistance"}  # -> {"adx", "bb_percent_b"}

# Emitted-but-not-gated keys: structural routing + context/AI-only technicals. Documented in
# pine/feature_exporter.contract.md. Anything emitted outside the known sets must be listed here.
ALLOWED_EXTRA = {"action", "symbol", "rsi", "atr_pct", "dist_sma200_pct",
                 "bb_width_pct", "don_high_20", "don_low_20",          # daily setup context
                 "i_rsi", "i_vol_ratio"}                               # intraday setup context
KNOWN = EXPECTED_KEYS | SETUP_CONSUMED

DAILY_SETUP_KEYS = {k for k in SETUP_CONSUMED if not k.startswith("i_")}
INTRADAY_SETUP_KEYS = {k for k in SETUP_CONSUMED if k.startswith("i_")}

pytestmark = pytest.mark.skipif(not PINE.exists(), reason="pine/ not present (e.g. ThetaBot sync)")


def _emitted_keys(path: Path = PINE) -> set[str]:
    src = path.read_text(encoding="utf-8")
    # The alert message builds JSON as string fragments like  '...,"adx":' + str.tostring(...).
    # Every emitted field is a "<key>": literal.
    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"\s*:', src))


def test_exporter_emits_owned_keys():
    emitted = _emitted_keys()
    missing = EXPORTER_OWNS - emitted
    assert not missing, (
        f"Feature Exporter no longer emits the keys it owns {missing}; their gates would silently "
        "stop firing. Fix the .pine emit or EXPORTER_OWNS."
    )
    # support/resistance must NOT be re-added here — they belong to the S/R-Channels script; emitting
    # them from both re-creates the timeframe collision (see contract doc).
    assert "support" not in emitted and "resistance" not in emitted, (
        "Feature Exporter emits support/resistance again — that collides with the 30m S/R-Channels "
        "feed. Remove them here (they are owned solely by that script)."
    )


def test_no_unexpected_emitted_keys():
    emitted = _emitted_keys()
    unexpected = emitted - KNOWN - ALLOWED_EXTRA
    assert not unexpected, (
        f"Pine exporter emits unrecognized keys {unexpected}. If intended, add them to the "
        "scanner contract (EXPECTED_KEYS / SETUP_CONSUMED) or to ALLOWED_EXTRA + the contract doc."
    )


def test_daily_exporter_owns_daily_setup_keys():
    missing = DAILY_SETUP_KEYS - _emitted_keys()
    assert not missing, (
        f"Feature Exporter no longer emits the daily setup keys {missing}; the TradingView setup "
        "overlay (parse_tv_setups) would silently see nothing."
    )


@pytest.mark.skipif(not SETUP_PINE.exists(), reason="pine/setup_exporter.pine not present")
def test_setup_exporter_owns_intraday_keys_and_nothing_else():
    emitted = _emitted_keys(SETUP_PINE)
    missing = INTRADAY_SETUP_KEYS - emitted
    assert not missing, f"Setup Exporter no longer emits {missing}"
    assert not (emitted & DAILY_SETUP_KEYS), (
        "Setup Exporter emits un-prefixed daily keys — they would collide with the Daily exporter in "
        "the merged snapshot. Intraday keys must be i_-prefixed."
    )
    assert "support" not in emitted and "resistance" not in emitted
    unexpected = emitted - KNOWN - ALLOWED_EXTRA
    assert not unexpected, f"Setup Exporter emits unrecognized keys {unexpected}"
    assert "token" not in emitted, "never put the webhook token in the alert message"
