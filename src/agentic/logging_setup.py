"""Centralized logging configuration."""
from __future__ import annotations

import logging
import sys


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, with a concise console format."""
    # Silence the MCP streamable-http notification-stream churn: the client reconnects that GET
    # stream (+ logs "Session termination failed: 400") every ~5s against the Robinhood agentic
    # server. This bot uses request/response tool calls, not the notification stream, so it's
    # harmless — but it floods the logs and buries real errors. Keep ERROR so genuine MCP failures
    # still surface. Set before the already-configured guard so it always applies.
    logging.getLogger("mcp.client.streamable_http").setLevel(logging.ERROR)
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
