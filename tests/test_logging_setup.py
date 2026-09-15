"""Logging setup: the noisy MCP streamable-http reconnect logger is silenced below ERROR."""
import logging

from agentic.logging_setup import setup_logging


def test_mcp_streamable_http_logger_quieted():
    # Applied before the already-configured guard, so it holds regardless of prior root config.
    setup_logging()
    assert logging.getLogger("mcp.client.streamable_http").level == logging.ERROR
