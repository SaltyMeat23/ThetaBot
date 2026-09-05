"""OAuth wiring for the Robinhood agentic trading MCP.

Robinhood authenticates agents via OAuth — there is no static bearer token. The mcp SDK's
``OAuthClientProvider`` does the hard parts (metadata discovery, dynamic client registration, the
PKCE authorization-code flow, and token *refresh*); we supply:

  * ``FileTokenStorage`` — persists the tokens + client registration to the data volume so the bot
    reuses them across restarts and refreshes headlessly.
  * ``build_provider`` — constructs the provider (interactive handlers only for the one-time login;
    the running bot passes none and simply refreshes).

Flow: run the desktop bootstrap once (``python -m agentic.tools.rh_login``) to obtain tokens into
``data/rh_oauth.json``, upload that file to the VPS volume, and the bot connects + auto-refreshes.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from ..config import REPO_ROOT, get_secret

log = logging.getLogger("agentic.brokers.rh_oauth")

DEFAULT_OAUTH_PATH = REPO_ROOT / "data" / "rh_oauth.json"

# Refresh the access token this many seconds BEFORE its hard expiry, so the proactive
# refresh_token grant fires on a still-valid session instead of after a 401.
_REFRESH_MARGIN_SECONDS = 300


def maybe_seed_oauth_from_env(path: Path = DEFAULT_OAUTH_PATH) -> bool:
    """First-boot seed from the RH_OAUTH_JSON env var into the writable data volume.

    Lets the token be delivered as a Coolify env var (simple UI paste) instead of hand-placing a
    file in the volume. The on-disk file is authoritative afterwards — refreshes/rotations persist
    there — so an EXISTING file is never overwritten (the env only seeds a first boot). Returns True
    if it wrote the file this call.
    """
    if path.exists():
        return False
    raw = get_secret("RH_OAUTH_JSON")
    if not raw:
        return False
    try:
        d = json.loads(raw)
    except Exception:  # noqa: BLE001 — malformed env value: skip, stay on paper
        return False
    if not (d.get("tokens") or {}).get("refresh_token") and not (d.get("tokens") or {}).get(
            "access_token"):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return True


def oauth_available(path: Path = DEFAULT_OAUTH_PATH) -> bool:
    """True when a stored token file with a usable access/refresh token exists."""
    try:
        d = json.loads(path.read_text("utf-8"))
    except Exception:  # noqa: BLE001 — missing/corrupt file -> not available
        return False
    tok = d.get("tokens") or {}
    return bool(tok.get("access_token") or tok.get("refresh_token"))


class FileTokenStorage:
    """mcp.client.auth.TokenStorage backed by a JSON file (tokens + client registration)."""

    def __init__(self, path: Path = DEFAULT_OAUTH_PATH):
        self.path = path

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _save(self, d: dict) -> None:
        # Atomic write: a token save interrupted mid-write (or racing another) must never leave a
        # half-written rh_oauth.json — a corrupt token file means a full manual re-auth. Write a
        # temp file, then os.replace() it into place (atomic on POSIX and Windows).
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        d = self._load().get("tokens")
        return OAuthToken.model_validate(d) if d else None

    async def set_tokens(self, tokens) -> None:
        d = self._load()
        d["tokens"] = tokens.model_dump(exclude_none=True)
        # Persist a wall-clock issue time alongside the token. The SDK's OAuthToken carries only a
        # relative ``expires_in`` (no absolute expiry), so without this a restarted process cannot
        # tell when the access token expires — it assumes valid, sends an expired token, and 401s
        # into an interactive re-auth. ``issued_at`` lets the provider reconstruct expiry on boot.
        d["issued_at"] = time.time()
        self._save(d)

    def issued_at(self) -> float | None:
        """Wall-clock time the stored token was last saved (for expiry reconstruction)."""
        v = self._load().get("issued_at")
        return float(v) if isinstance(v, (int, float)) else None

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        d = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(d) if d else None

    async def set_client_info(self, client_info) -> None:
        d = self._load()
        d["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save(d)


def client_metadata(redirect_uri: str):
    """Public-client (PKCE) registration metadata for the bot."""
    from mcp.shared.auth import OAuthClientMetadata
    return OAuthClientMetadata(
        client_name="AgenticRobinhood",
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


async def _discover_server_metadata(server_url: str) -> tuple[Any, Any]:
    """Fetch RH's OAuth authorization-server + protected-resource metadata (unauthenticated).

    Returns (oauth_metadata, protected_resource_metadata); either may be None on failure.

    The MCP SDK only discovers these during an interactive 401 flow and never persists them, so a
    HEADLESS refresh would otherwise POST to the SDK's fallback endpoint (``server_base + /token``)
    instead of RH's real token endpoint (``https://api.robinhood.com/oauth2/token/``). Restoring the
    metadata on init makes the refresh_token grant hit the correct endpoint with the resource param,
    exactly as the working interactive login does. Best-effort: on any error we return Nones and the
    SDK keeps its default behavior.
    """
    import httpx
    from mcp.shared.auth import OAuthMetadata, ProtectedResourceMetadata

    parsed = urlparse(server_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.strip("/")  # e.g. "mcp/trading" -> resource-scoped PRM discovery
    asm: Any = None
    prm: Any = None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(urljoin(base, "/.well-known/oauth-authorization-server"))
            if r.status_code == 200:
                asm = OAuthMetadata.model_validate_json(r.content)
            prm_url = urljoin(base, f"/.well-known/oauth-protected-resource/{path}" if path
                              else "/.well-known/oauth-protected-resource")
            rp = await client.get(prm_url)
            if rp.status_code == 200:
                prm = ProtectedResourceMetadata.model_validate_json(rp.content)
    except Exception as exc:  # noqa: BLE001 — discovery is best-effort; refresh falls back to default
        log.warning("OAuth metadata discovery failed (%s); refresh uses SDK default endpoint.", exc)
    return asm, prm


_PROVIDER_CLASS: Any = None


def _provider_class() -> Any:
    """Lazily build the hardened OAuthClientProvider subclass (keeps the ``mcp`` import optional)."""
    global _PROVIDER_CLASS
    if _PROVIDER_CLASS is not None:
        return _PROVIDER_CLASS
    from mcp.client.auth import OAuthClientProvider

    class _HeadlessRefreshProvider(OAuthClientProvider):
        """OAuthClientProvider hardened for headless token refresh across process restarts.

        The stock SDK (1.28.x) never restores two things from storage on init, which together make
        headless refresh impossible:

          1. Access-token EXPIRY — ``is_token_valid()`` treats any stored token as valid (expiry is
             None), so the proactive refresh_token branch never fires; an expired token then 401s
             straight into an interactive re-auth (``OAuthFlowError`` headless).
          2. Discovered SERVER METADATA — a refresh would POST to ``server_base + /token`` instead
             of RH's real token endpoint.

        This subclass restores both on every (re)initialize, so refresh happens automatically, on a
        still-valid session, against the correct endpoint.
        """

        async def _initialize(self) -> None:
            await super()._initialize()
            ctx = self.context
            # (1) Reconstruct expiry from the persisted issue time.
            toks = ctx.current_tokens
            issued_at: float | None = None
            getter = getattr(ctx.storage, "issued_at", None)
            if callable(getter):
                issued_at = getter()
            if toks and toks.access_token and toks.expires_in:
                if issued_at:
                    ctx.token_expiry_time = issued_at + int(toks.expires_in) - _REFRESH_MARGIN_SECONDS
                else:
                    # Unknown age: mark expired so we refresh now rather than 401 -> interactive
                    # re-auth. Must be a truthy past time, NOT 0.0 — is_token_valid() reads
                    # ``not token_expiry_time``, so 0.0 (falsy) would read as "no expiry" = valid,
                    # reintroducing the very bug this fixes.
                    ctx.token_expiry_time = 1.0
            # (2) Restore server metadata so a headless refresh uses the real token endpoint.
            if ctx.oauth_metadata is None:
                asm, prm = await _discover_server_metadata(ctx.server_url)
                if asm is not None:
                    ctx.oauth_metadata = asm
                if prm is not None:
                    ctx.protected_resource_metadata = prm

    _PROVIDER_CLASS = _HeadlessRefreshProvider
    return _PROVIDER_CLASS


def build_provider(
    server_url: str,
    storage: FileTokenStorage,
    *,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]] | None = None,
    redirect_uri: str = "http://localhost:8765/callback",
) -> Any:
    """OAuthClientProvider for use as ``streamablehttp_client(auth=...)``.

    Returns the hardened ``_HeadlessRefreshProvider`` so the running bot renews access tokens
    non-interactively across restarts (see that class). A full re-authorization (handlers required)
    only happens at bootstrap via ``rh_login``.
    """
    cls = _provider_class()
    return cls(
        server_url=server_url,
        client_metadata=client_metadata(redirect_uri),
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
