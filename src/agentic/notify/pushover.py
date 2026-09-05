"""Pushover notifier.

Sends push notifications via the Pushover messages API. Needs PUSHOVER_TOKEN (your
application API token) and PUSHOVER_USER (your user/group key) in the environment.

Pushover priorities: -2..2. We map our levels: low->-1, normal->0, high->1, urgent->2.
Approval ``actions`` (Phase 3) are surfaced via a supplementary URL on the message;
Pushover has no inline buttons, so the URL deep-links to the approval page.
Falls back to logging if creds or httpx are missing, so the app never crashes on notify.
"""
from __future__ import annotations

import logging

from ..config import get_secret
from .base import Notifier

log = logging.getLogger("agentic.notify.pushover")

_API = "https://api.pushover.net/1/messages.json"
_PRIORITY = {"low": -1, "normal": 0, "high": 1, "urgent": 2}


class PushoverNotifier(Notifier):
    def __init__(self) -> None:
        self._token = get_secret("PUSHOVER_TOKEN")
        self._user = get_secret("PUSHOVER_USER")
        try:
            import httpx  # noqa: F401
            self._have_httpx = True
        except ImportError:
            self._have_httpx = False

    async def send(self, title, message, *, priority="normal", actions=None) -> None:
        if not (self._token and self._user and self._have_httpx):
            log.info("NOTIFY(pushover-fallback) [%s] %s — %s", priority, title, message)
            return
        import httpx

        data = {
            "token": self._token,
            "user": self._user,
            "title": title,
            "message": message,
            "priority": _PRIORITY.get(priority, 0),
        }
        if priority == "urgent":
            data["retry"] = 60
            data["expire"] = 3600
        if actions:
            # Surface the first action's URL as a clickable supplementary link.
            first = actions[0]
            if first.get("url"):
                data["url"] = first["url"]
                data["url_title"] = first.get("label", "Open")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(_API, data=data)
                resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001 — never crash on a notification failure
            log.warning("Pushover send failed: %s", exc)
