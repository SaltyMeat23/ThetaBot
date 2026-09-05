"""Centralized logging configuration."""
from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with a concise console format."""
    root = logging.getLogger()
    if root.handlers:  # already configured
        root.setLevel(level)
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.setLevel(level)
    # Quiet noisy third-party loggers.
    for noisy in ("httpx", "httpcore", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
