"""HTTP Basic Auth gate for the dashboard + data/control endpoints.

Auth is ACTIVE only when ``DASHBOARD_PASSWORD`` is set (env / Coolify secret); otherwise the
dependency is a no-op so local dev and tests run open. Username defaults to ``admin`` and can
be overridden with ``DASHBOARD_USER``. Comparisons are constant-time.

Applied to: /dashboard, /, /api/*, and /control/status|pause|resume. NOT applied to /health
(Coolify healthcheck), /webhook/tradingview (own shared-secret token), or the UUID-guarded
/control/approve|reject links (so phone notification taps keep working).
"""
from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..config import get_secret

_basic = HTTPBasic(auto_error=False)


def auth_enabled() -> bool:
    return bool(get_secret("DASHBOARD_PASSWORD"))


def require_auth(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    """FastAPI dependency: enforce Basic Auth when a password is configured."""
    password = get_secret("DASHBOARD_PASSWORD")
    if not password:
        return  # auth disabled — no password configured
    username = get_secret("DASHBOARD_USER", "admin")
    ok = credentials is not None and hmac.compare_digest(
        credentials.username, username
    ) and hmac.compare_digest(credentials.password, password)
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
