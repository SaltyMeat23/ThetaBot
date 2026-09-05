"""Build the configured notifier."""
from __future__ import annotations

from ..config import Settings
from .base import ConsoleNotifier, Notifier
from .pushover import PushoverNotifier


def build_notifier(settings: Settings) -> Notifier:
    if settings.notify.provider == "pushover":
        return PushoverNotifier()
    # 'ntfy' is added in Phase 3 (one-tap approval buttons); default to console for now.
    return ConsoleNotifier()
