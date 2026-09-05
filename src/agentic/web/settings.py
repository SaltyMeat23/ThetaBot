"""In-app settings editor — read and safely edit strategy/paper params from the dashboard.

  GET  /api/config   -> current effective config (editable knobs + a read-only summary)
  POST /api/config   -> validate a patch, hot-apply it, and persist it to the overlay

Design constraints (see docs/STAGE2_PLAN.md):

* HARD GUARDRAIL — the editor may only touch strategy/paper params. It can NEVER change
  ``mode``, ``i_understand_live_trading``, ``broker``, ``broker_fallback``, ``market_data`` or
  ``robinhood`` (account/live-arming/execution-path). Arming live stays a deliberate file/env
  action, never a button.
* HOT-APPLY IN PLACE — services share one ``Settings`` object by reference, and ``RiskSizer``
  captures ``settings.entry.sizing`` by reference at init. So edits mutate nested *leaf* fields
  in place and never replace a sub-model, or the running sizer would keep a stale reference.
* PERSISTED to the writable overlay (``config.OVERLAY_PATH``), not the read-only mounted base,
  so edits survive a redeploy.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from ..config import Settings, load_overlay, save_overlay
from .auth import require_auth

if TYPE_CHECKING:
    from .app import WebDeps

log = logging.getLogger("agentic.web.settings")

# Allowlist (default-deny): only these top-level keys may be edited via the API. Everything else —
# notably mode, i_understand_live_trading, broker, broker_fallback, market_data, robinhood, web —
# is immutable at runtime.
EDITABLE_TOP_LEVEL = frozenset({
    "paper_buying_power",
    "paper_seed_positions",
    "poll_interval_seconds",
    "poll_interval_closed_seconds",
    "reconcile_interval_seconds",
    "approval_timeout_seconds",
    "max_quote_age_seconds",
    "auto_trip_after_errors",
    "execution",
    "entry",
    "macro",
    "ai",
    "news",
    "roll",
    "reporting",
    "notify",
    "rules",
})


class SettingsEditError(ValueError):
    """Raised when a patch is rejected (protected key or invalid value)."""


def _reject_protected(patch: dict) -> None:
    bad = [k for k in patch if k not in EDITABLE_TOP_LEVEL]
    if bad:
        raise SettingsEditError(
            f"These settings are not editable at runtime: {', '.join(sorted(bad))}. "
            "mode / live-arming / broker / account changes must be made deliberately in the "
            "mounted config or environment."
        )


def _apply_in_place(live: BaseModel, validated: BaseModel, patch: dict) -> None:
    """Copy patched leaves from ``validated`` onto ``live`` IN PLACE.

    Recurses into sub-models rather than replacing them, so references captured elsewhere
    (e.g. RiskSizer's ``settings.entry.sizing``) see the new values.
    """
    for key, val in patch.items():
        cur = getattr(live, key)
        if isinstance(val, dict) and isinstance(cur, BaseModel):
            _apply_in_place(cur, getattr(validated, key), val)
        else:
            setattr(live, key, getattr(validated, key))


def apply_patch(settings: Settings, patch: dict, *, overlay_path=None) -> list[str]:
    """Validate, hot-apply (in place), and persist a settings patch. Returns changed top keys.

    Raises SettingsEditError on a protected key or a value that fails Settings validation.
    """
    if not isinstance(patch, dict) or not patch:
        raise SettingsEditError("Request body must be a non-empty object of settings to change.")
    _reject_protected(patch)

    from ..config import _deep_merge  # local import to avoid a public surface for the helper

    merged = _deep_merge(settings.model_dump(), patch)
    try:
        validated = Settings.model_validate(merged)
    except ValidationError as exc:
        raise SettingsEditError(f"Invalid setting value(s): {exc.errors()}") from exc

    _apply_in_place(settings, validated, patch)

    overlay = _deep_merge(load_overlay(overlay_path), patch)
    save_overlay(overlay, overlay_path)
    log.info("Settings edited via API: %s", sorted(patch))
    return sorted(patch)


def _editable_view(settings: Settings) -> dict[str, Any]:
    dump = settings.model_dump(mode="json")
    return {k: dump[k] for k in EDITABLE_TOP_LEVEL if k in dump}


def make_settings_router(deps: "WebDeps") -> APIRouter:
    router = APIRouter(prefix="/api")

    @router.get("/config", dependencies=[Depends(require_auth)])
    async def get_config() -> dict:
        s = deps.settings
        return {
            "editable": _editable_view(s),
            # Read-only context so the UI can SHOW (never edit) the safety-critical state.
            "readonly": {
                "mode": s.mode,
                "live_armed": s.is_live,
                "broker": s.broker,
                "broker_fallback": s.broker_fallback,
                "market_data": s.market_data,
                "account_number": s.robinhood.account_number,
            },
        }

    @router.post("/config", dependencies=[Depends(require_auth)])
    async def post_config(patch: dict = Body(...)) -> JSONResponse:
        try:
            changed = apply_patch(deps.settings, patch)
        except SettingsEditError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "changed": changed, "editable": _editable_view(deps.settings)})

    return router
