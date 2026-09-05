"""One-time interactive Robinhood OAuth bootstrap (run on a DESKTOP).

Robinhood's agentic auth is desktop-only and bounces through your browser + the RH mobile app.
This runs that flow once and writes tokens (access + refresh) to ``data/rh_oauth.json``. Upload that
file to the VPS data volume (``/app/data/rh_oauth.json``) and the bot connects + auto-refreshes.

    python -m agentic.tools.rh_login

Requires the 'mcp' extra and a primary + agentic Robinhood account already set up.
"""
from __future__ import annotations

import asyncio
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from ..brokers.rh_oauth import FileTokenStorage, build_provider
from ..brokers.robinhood_mcp import MCP_URL

_PORT = 8765
_REDIRECT_URI = f"http://localhost:{_PORT}/callback"


class _CallbackCatcher:
    """Tiny localhost server that captures the OAuth ?code=&state= redirect."""

    def __init__(self, port: int):
        self.port = port
        self.result: dict = {}
        self._event = threading.Event()
        self._srv: HTTPServer | None = None

    def start(self) -> None:
        catcher = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = parse_qs(urlparse(self.path).query)
                catcher.result = {k: q.get(k, [None])[0] for k in ("code", "state", "error")}
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<h2>AgenticRobinhood: authorization received.</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>")
                catcher._event.set()

            def log_message(self, *_a):  # silence access logs
                return

        self._srv = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def wait(self, timeout: float = 300.0) -> dict:
        self._event.wait(timeout)
        return self.result

    def stop(self) -> None:
        if self._srv is not None:
            try:
                self._srv.shutdown()
            except Exception:  # noqa: BLE001
                pass


async def main() -> int:
    storage = FileTokenStorage()
    catcher = _CallbackCatcher(_PORT)
    catcher.start()

    async def redirect_handler(url: str) -> None:
        print("\n1) Authorize in your browser (opening now; desktop only):\n   " + url + "\n"
              "2) Log in to Robinhood and approve — you'll verify on the RH mobile app.")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless: user opens the printed URL manually
            pass

    async def callback_handler() -> tuple[str, str | None]:
        loop = asyncio.get_event_loop()
        res = await loop.run_in_executor(None, catcher.wait, 300.0)
        if not res or res.get("error") or not res.get("code"):
            raise RuntimeError(f"OAuth callback failed: {res.get('error') if res else 'timeout'}")
        return res["code"], res.get("state")

    provider = build_provider(
        MCP_URL, storage, redirect_handler=redirect_handler,
        callback_handler=callback_handler, redirect_uri=_REDIRECT_URI)

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    print("Connecting to Robinhood agentic MCP …")
    try:
        async with streamablehttp_client(MCP_URL, auth=provider) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
    finally:
        catcher.stop()

    print(f"\n[OK] Authorized — {len(tools.tools)} tools available. Tokens saved to:\n  {storage.path}")
    print("\nEASIEST DEPLOY: set this as a Coolify env var named  RH_OAUTH_JSON  (one line):\n")
    try:
        import json as _json
        print("  " + _json.dumps(_json.loads(storage.path.read_text("utf-8")), separators=(",", ":")))
    except Exception:  # noqa: BLE001
        print("  (could not compact; upload the file to /app/data/rh_oauth.json instead)")
    print("\nThen redeploy (mode:live). The bot seeds the writable volume on first boot and "
          "auto-refreshes from there. (Alternatively, place the file at /app/data/rh_oauth.json.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
