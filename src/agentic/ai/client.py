"""Anthropic client wrapper for the AI reviewer.

`anthropic` is an optional dependency (the ``ai`` extra) imported lazily, so the core service runs
without it. build_reviewer_client returns None when AI is disabled / unkeyed / the extra is missing,
so the caller fails open to the rules.
"""
from __future__ import annotations

import json
import logging
from typing import Protocol

from ..config import AIConfig, get_secret
from .schema import VERDICT_SCHEMA

log = logging.getLogger("agentic.ai.client")


class ReviewerClient(Protocol):
    async def analyze(self, system: str, user: str) -> dict: ...


class AnthropicReviewerClient:
    """Calls Claude with a forced JSON-schema verdict (async)."""

    def __init__(self, model: str, effort: str = "medium"):
        import anthropic  # lazy — only when AI is enabled and the 'ai' extra is installed

        self._client = anthropic.AsyncAnthropic(api_key=get_secret("ANTHROPIC_API_KEY"))
        self._model = model
        self._effort = effort

    async def analyze(self, system: str, user: str) -> dict:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=2000,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": VERDICT_SCHEMA},
            },
        )
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), "")
        return json.loads(text) if text else {}

    async def summarize(self, system: str, user: str, max_tokens: int = 400) -> str:
        """Plain-text completion (no forced schema) — for prose like the weekly summary."""
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        ).strip()


def build_reviewer_client(cfg: AIConfig) -> ReviewerClient | None:
    """Construct the Anthropic client if AI is enabled and usable; else None (fail-open)."""
    if not cfg.enabled:
        return None
    if not get_secret("ANTHROPIC_API_KEY"):
        log.warning("ai.enabled=true but ANTHROPIC_API_KEY is not set — AI review disabled.")
        return None
    try:
        return AnthropicReviewerClient(cfg.model, cfg.effort)
    except Exception as exc:  # noqa: BLE001 — missing 'anthropic' extra, bad key, etc.
        log.warning("AI reviewer client unavailable (%s); AI review disabled.", exc)
        return None
