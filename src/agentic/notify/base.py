"""Notifier interface + a console fallback.

In Phase 3 the ``actions`` argument carries approve/reject callback URLs for one-tap
approval; Phase 1 only sends plain alerts, so notifiers may ignore ``actions``.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod

log = logging.getLogger("agentic.notify")


class Notifier(ABC):
    @abstractmethod
    async def send(
        self,
        title: str,
        message: str,
        *,
        priority: str = "normal",
        actions: list[dict] | None = None,
    ) -> None:
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    """Logs notifications. Used when notify.provider == 'none' or as a safe default."""

    async def send(self, title, message, *, priority="normal", actions=None) -> None:
        log.info("NOTIFY [%s] %s — %s", priority, title, message)
