"""Refinement export: one flat feature->outcome table for tuning entry logic.

The entry features + realized outcome live in the trade journal; the AI verdict lives in
ai_reviews; they share the entry-decision id. This joins them into a single rectangular row
per trade (CSV- or JSON-friendly), so the labeled dataset can be pulled in one call instead of
hand-joining three endpoints.
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

# Explicit, stable column order — keeps the CSV rectangular across releases.
COLUMNS = [
    "entered_at", "closed_at", "underlying", "occ_symbol", "kind", "contracts", "strike", "dte",
    "delta", "iv", "premium", "spread_pct", "open_interest", "volume", "annualized_ror",
    "underlying_price", "status", "realized_pnl", "close_price", "days_held", "exit_reason",
    "ai_recommendation", "ai_confidence", "ai_move_class", "ai_regime_label", "ai_model",
    "ai_flags", "entry_decision_id", "context",
]


def build_refinement_rows(trade_journal, ai_reviews, limit: int = 1000) -> list[dict[str, Any]]:
    """Join journal trades with their AI verdict into flat rows (newest first)."""
    ai_by_decision = ai_reviews.by_decision() if ai_reviews is not None else {}
    rows: list[dict[str, Any]] = []
    for e in trade_journal.recent(limit):
        ai = ai_by_decision.get(e.entry_decision_id, {})
        rows.append({
            "entered_at": e.entered_at.isoformat() if e.entered_at else None,
            "closed_at": e.closed_at.isoformat() if e.closed_at else None,
            "underlying": e.underlying, "occ_symbol": e.occ_symbol, "kind": e.kind,
            "contracts": e.contracts, "strike": e.strike, "dte": e.dte, "delta": e.delta,
            "iv": e.iv, "premium": e.premium, "spread_pct": e.spread_pct,
            "open_interest": e.open_interest, "volume": e.volume,
            "annualized_ror": e.annualized_ror, "underlying_price": e.underlying_price,
            "status": e.status, "realized_pnl": e.realized_pnl, "close_price": e.close_price,
            "days_held": e.days_held, "exit_reason": e.exit_reason,
            "ai_recommendation": ai.get("ai_recommendation"),
            "ai_confidence": ai.get("ai_confidence"),
            "ai_move_class": ai.get("ai_move_class"),
            "ai_regime_label": ai.get("ai_regime_label"),
            "ai_model": ai.get("ai_model"),
            "ai_flags": ai.get("ai_flags", []),
            "entry_decision_id": e.entry_decision_id,
            "context": e.context or {},
        })
    return rows


def rows_to_csv(rows: list[dict[str, Any]]) -> str:
    """Serialize rows to CSV with the fixed COLUMNS order; list/dict cells become JSON strings."""
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        out = {}
        for k in COLUMNS:
            v = r.get(k)
            out[k] = json.dumps(v, default=str) if isinstance(v, (list, dict)) else v
        w.writerow(out)
    return buf.getvalue()
