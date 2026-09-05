"""Structured verdict for the AI trade-analyst — forced JSON schema + a defensive normalizer."""
from __future__ import annotations

from dataclasses import dataclass, field

RECOMMENDATIONS = ("take", "caution", "skip")

# Forced on the model via output_config.format so the response is always this exact shape.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "recommendation": {"type": "string", "enum": list(RECOMMENDATIONS)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "flags": {"type": "array", "items": {"type": "string"}},
        "regime_read": {"type": "string"},
    },
    "required": ["recommendation", "confidence", "rationale", "flags", "regime_read"],
    "additionalProperties": False,
}


@dataclass
class Verdict:
    recommendation: str                    # take | caution | skip
    confidence: float                      # 0..1
    rationale: str = ""
    flags: list[str] = field(default_factory=list)
    regime_read: str = ""

    def as_dict(self) -> dict:
        return {
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "flags": list(self.flags),
            "regime_read": self.regime_read,
        }


def normalize_verdict(raw: dict) -> Verdict:
    """Coerce a raw model response into a safe Verdict. Unknown/garbage -> conservative 'caution'."""
    rec = str(raw.get("recommendation", "")).lower().strip()
    if rec not in RECOMMENDATIONS:
        rec = "caution"                    # never silently upgrade an unknown answer to "take"
    try:
        conf = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        conf = 0.0
    conf = max(0.0, min(1.0, conf))
    flags = raw.get("flags") or []
    if not isinstance(flags, list):
        flags = [str(flags)]
    return Verdict(
        recommendation=rec,
        confidence=conf,
        rationale=str(raw.get("rationale", "")),
        flags=[str(f) for f in flags],
        regime_read=str(raw.get("regime_read", "")),
    )
