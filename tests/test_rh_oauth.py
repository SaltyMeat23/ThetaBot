"""Robinhood OAuth wiring: persistent token storage, availability detection, provider build."""
import json
import time

import pytest

pytest.importorskip("mcp")  # OAuth wiring needs the 'mcp' extra

import agentic.brokers.rh_oauth as rh_oauth  # noqa: E402
from agentic.brokers.rh_oauth import (  # noqa: E402
    FileTokenStorage, build_provider, client_metadata, oauth_available,
)

RH_URL = "https://agent.robinhood.com/mcp/trading"


async def _seed_token_file(path, *, expires_in=3600, issued_at=None):
    """Write a token file the SDK can load (tokens + client_info), optionally with issued_at."""
    d = {
        "tokens": {"access_token": "acc", "token_type": "Bearer",
                   "refresh_token": "ref", "expires_in": expires_in},
        "client_info": {"client_id": "cid",
                        "redirect_uris": ["http://localhost:8765/callback"],
                        "token_endpoint_auth_method": "none"},
    }
    if issued_at is not None:
        d["issued_at"] = issued_at
    path.write_text(json.dumps(d))


async def _no_discovery(_url):
    return None, None


@pytest.mark.asyncio
async def test_token_storage_roundtrip(tmp_path):
    from mcp.shared.auth import OAuthToken

    s = FileTokenStorage(tmp_path / "rh_oauth.json")
    assert await s.get_tokens() is None                      # empty to start
    tok = OAuthToken(access_token="acc", token_type="Bearer", refresh_token="ref", expires_in=3600)
    await s.set_tokens(tok)
    got = await s.get_tokens()
    assert got.access_token == "acc" and got.refresh_token == "ref"
    # Persisted to disk, reloadable by a fresh storage instance (survives restart).
    assert (await FileTokenStorage(tmp_path / "rh_oauth.json").get_tokens()).refresh_token == "ref"


@pytest.mark.asyncio
async def test_client_info_roundtrip(tmp_path):
    from mcp.shared.auth import OAuthClientInformationFull

    s = FileTokenStorage(tmp_path / "rh_oauth.json")
    assert await s.get_client_info() is None
    info = OAuthClientInformationFull(
        client_id="cid", redirect_uris=["http://localhost:8765/callback"])
    await s.set_client_info(info)
    assert (await s.get_client_info()).client_id == "cid"


def test_oauth_available(tmp_path):
    p = tmp_path / "rh_oauth.json"
    assert not oauth_available(p)                             # no file
    p.write_text(json.dumps({"tokens": {"access_token": "a"}}))
    assert oauth_available(p)                                 # has an access token
    p.write_text(json.dumps({"tokens": {"refresh_token": "r"}}))
    assert oauth_available(p)                                 # refresh alone counts (can renew)
    p.write_text(json.dumps({"tokens": {}}))
    assert not oauth_available(p)                             # present but empty


def test_client_metadata_is_public_pkce():
    m = client_metadata("http://localhost:8765/callback")
    assert m.token_endpoint_auth_method == "none"            # public client
    assert "refresh_token" in m.grant_types                  # able to renew headlessly


def test_build_provider_constructs(tmp_path):
    prov = build_provider(
        "https://agent.robinhood.com/mcp/trading", FileTokenStorage(tmp_path / "rh_oauth.json"))
    assert prov is not None                                  # usable as streamablehttp_client(auth=)


def test_seed_from_env_first_boot_only(tmp_path, monkeypatch):
    from agentic.brokers.rh_oauth import maybe_seed_oauth_from_env
    p = tmp_path / "rh_oauth.json"
    monkeypatch.delenv("RH_OAUTH_JSON", raising=False)
    assert maybe_seed_oauth_from_env(p) is False                 # no env + no file -> nothing
    monkeypatch.setenv("RH_OAUTH_JSON", json.dumps({"tokens": {"refresh_token": "r"}}))
    assert maybe_seed_oauth_from_env(p) is True                  # seeds on first boot
    assert oauth_available(p)
    p.write_text(json.dumps({"tokens": {"refresh_token": "ROTATED"}}))
    assert maybe_seed_oauth_from_env(p) is False                 # never overwrites an existing file
    assert "ROTATED" in p.read_text()                            # rotation preserved


def test_seed_skips_garbage_or_empty(tmp_path, monkeypatch):
    from agentic.brokers.rh_oauth import maybe_seed_oauth_from_env
    p = tmp_path / "rh_oauth.json"
    monkeypatch.setenv("RH_OAUTH_JSON", "not json")
    assert maybe_seed_oauth_from_env(p) is False and not p.exists()
    monkeypatch.setenv("RH_OAUTH_JSON", json.dumps({"tokens": {}}))   # no usable token
    assert maybe_seed_oauth_from_env(p) is False and not p.exists()


# --- headless-refresh hardening (expiry + metadata restoration) ----------------------------------

@pytest.mark.asyncio
async def test_set_tokens_persists_issued_at(tmp_path):
    from mcp.shared.auth import OAuthToken
    s = FileTokenStorage(tmp_path / "rh_oauth.json")
    assert s.issued_at() is None
    before = time.time()
    await s.set_tokens(OAuthToken(access_token="a", token_type="Bearer",
                                  refresh_token="r", expires_in=3600))
    ia = s.issued_at()
    assert ia is not None and ia >= before - 1
    # survives a fresh storage instance (i.e. a process restart)
    assert FileTokenStorage(tmp_path / "rh_oauth.json").issued_at() is not None


@pytest.mark.asyncio
async def test_provider_valid_token_stays_valid(tmp_path, monkeypatch):
    monkeypatch.setattr(rh_oauth, "_discover_server_metadata", _no_discovery)
    p = tmp_path / "rh_oauth.json"
    await _seed_token_file(p, expires_in=3600, issued_at=time.time())  # just issued
    prov = build_provider(RH_URL, FileTokenStorage(p))
    await prov._initialize()
    assert prov.context.token_expiry_time is not None            # expiry reconstructed
    assert prov.context.is_token_valid() is True                 # fresh -> no refresh needed


@pytest.mark.asyncio
async def test_provider_expired_token_triggers_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(rh_oauth, "_discover_server_metadata", _no_discovery)
    p = tmp_path / "rh_oauth.json"
    await _seed_token_file(p, expires_in=3600, issued_at=time.time() - 7200)  # 2h old, 1h ttl
    prov = build_provider(RH_URL, FileTokenStorage(p))
    await prov._initialize()
    # This is the core fix: an expired stored token is now recognized as invalid, so the SDK's
    # proactive refresh_token branch fires instead of 401-ing into an interactive re-auth.
    assert prov.context.is_token_valid() is False
    assert prov.context.can_refresh_token() is True


@pytest.mark.asyncio
async def test_provider_missing_issued_at_forces_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(rh_oauth, "_discover_server_metadata", _no_discovery)
    p = tmp_path / "rh_oauth.json"
    await _seed_token_file(p, expires_in=3600, issued_at=None)  # legacy token, unknown age
    prov = build_provider(RH_URL, FileTokenStorage(p))
    await prov._initialize()
    assert prov.context.token_expiry_time == 1.0                 # truthy past time (not 0.0!)
    assert prov.context.is_token_valid() is False                # -> refresh, never 401->interactive


@pytest.mark.asyncio
async def test_provider_restores_real_token_endpoint(tmp_path, monkeypatch):
    from mcp.shared.auth import OAuthMetadata
    asm = OAuthMetadata.model_validate({
        "issuer": "https://agent.robinhood.com/mcp/trading",
        "authorization_endpoint": "https://robinhood.com/oauth",
        "token_endpoint": "https://api.robinhood.com/oauth2/token/",
        "registration_endpoint": "https://agent.robinhood.com/oauth/trading/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
    })

    async def _disc(_url):
        return asm, None

    monkeypatch.setattr(rh_oauth, "_discover_server_metadata", _disc)
    p = tmp_path / "rh_oauth.json"
    await _seed_token_file(p, expires_in=3600, issued_at=time.time())
    prov = build_provider(RH_URL, FileTokenStorage(p))
    await prov._initialize()
    # Without this, a headless refresh would POST to agent.robinhood.com/token (wrong host).
    assert str(prov.context.oauth_metadata.token_endpoint) == "https://api.robinhood.com/oauth2/token/"
