"""Pine 'Feature Exporter' contract guard.

Asserts the field names the versioned Pine alert emits still cover exactly what the bot's scanner
consumes. If the exporter renames a key (e.g. bb_percent_b -> percent_b) or the scanner's expected
key set changes without the other, this fails — killing the silent fail-open coupling.

Skips when pine/feature_exporter.pine is absent: scripts/sync_to_thetabot.sh copies only src/+tests/,
so the public ThetaBot repo won't have pine/.
"""
import re
from pathlib import Path

import pytest

from agentic.tools.tv_reconcile import EXPECTED_KEYS, SCANNER_CONSUMED

PINE = Path(__file__).resolve().parents[1] / "pine" / "feature_exporter.pine"

# Of the scanner-consumed keys, `support` (and `resistance`) are emitted by the SEPARATE 30m
# "Support Resistance Channels" script, not this Daily exporter. So this file only needs to emit the
# technical gate fields it owns. See pine/feature_exporter.contract.md ("two alerts merge").
EXPORTER_OWNS = SCANNER_CONSUMED - {"support", "resistance"}  # -> {"adx", "bb_percent_b"}

# Emitted-but-not-gated keys: structural routing + context/AI-only technicals. Documented in
# pine/feature_exporter.contract.md. Anything emitted outside EXPECTED_KEYS must be listed here.
ALLOWED_EXTRA = {"action", "symbol", "rsi", "atr_pct", "dist_sma200_pct"}

pytestmark = pytest.mark.skipif(not PINE.exists(), reason="pine/ not present (e.g. ThetaBot sync)")


def _emitted_keys() -> set[str]:
    src = PINE.read_text(encoding="utf-8")
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
    unexpected = emitted - EXPECTED_KEYS - ALLOWED_EXTRA
    assert not unexpected, (
        f"Pine exporter emits unrecognized keys {unexpected}. If intended, add them to the "
        "scanner contract (EXPECTED_KEYS) or to ALLOWED_EXTRA + the contract doc."
    )
