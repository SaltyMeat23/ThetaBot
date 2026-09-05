"""AI-written weekly summary for the operator's push report.

Advisory and fail-open: any problem returns None and the report falls back to the deterministic
numbers. Uses the same Anthropic client as the reviewer, but a plain-text completion (no schema).
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger("agentic.ai.weekly")

SYSTEM = (
    "You are the analyst for a cash-secured-put options wheel bot, writing the weekly summary the "
    "operator reads on their phone. Be concrete, honest, and brief: 3-4 plain sentences, no "
    "markdown, no bullet points, no preamble or sign-off. Cover how the week went (realized P&L and "
    "win rate), what drove it (which exit rules), the state of the open positions, and the single "
    "most useful thing to watch next week. Never invent numbers beyond those given; if a loss or "
    "risk is present, name it plainly rather than spinning it."
)


async def generate_weekly_summary(client, *, week_stats: dict, cumulative: dict,
                                  rows: list[dict]) -> str | None:
    """Return a short prose summary of the week, or None on any error / missing client."""
    if client is None or not hasattr(client, "summarize"):
        return None
    open_rows = [r for r in rows if r["status"] in ("OPEN", "CLOSING")]
    payload = {
        "this_week": {k: week_stats.get(k) for k in (
            "realized_pnl", "wins", "losses", "assigned", "win_rate",
            "credit_collected_resolved", "by_rule")},
        "since_inception": {k: cumulative.get(k) for k in (
            "realized_pnl", "resolved_count", "win_rate")},
        "open_positions": [
            {"symbol": r["underlying"], "strike": r["strike"], "dte": r["dte"],
             "unrealized_pnl": r["unrealized_pnl"]}
            for r in open_rows],
    }
    user = "Weekly data (JSON):\n" + json.dumps(payload, default=str) + "\n\nWrite the summary."
    try:
        text = await client.summarize(SYSTEM, user)
        return text or None
    except Exception as exc:  # noqa: BLE001 — advisory; the report sends without it
        log.warning("Weekly AI summary generation failed: %s", exc)
        return None
