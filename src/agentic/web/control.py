"""Control endpoints: one-tap approval and the kill switch.

  POST /control/approve/{decision_id}   -> approve a parked close (executes it)
  POST /control/reject/{decision_id}    -> reject it
  POST /control/pause                    -> engage the kill switch
  POST /control/resume                   -> release it
  GET  /control/status                   -> current control state

These are POSTed by the notification action buttons. They are protected only by the
unguessable decision id and (in production) by the tunnel; pause/resume optionally take a
``?token=`` matching CONTROL_TOKEN when set.
"""
from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ..config import get_secret
from ..domain.enums import AuditEventType
from .auth import require_auth

if TYPE_CHECKING:
    from .app import WebDeps

log = logging.getLogger("agentic.web.control")


def make_control_router(deps: "WebDeps") -> APIRouter:
    router = APIRouter(prefix="/control")

    def _control_authorized(token: str | None) -> bool:
        expected = get_secret("CONTROL_TOKEN")
        if not expected:
            return True  # no token configured -> rely on tunnel/network protection
        return bool(token) and hmac.compare_digest(token, expected)

    def _close_action_authorized(decision_id: str, token: str | None) -> bool:
        # Buying to close a real position must not be unauthenticated: require the per-decision
        # token (refuse if no CONTROL_TOKEN or on mismatch).
        from ..config import close_action_token
        expected = close_action_token(decision_id)
        return bool(expected) and bool(token) and hmac.compare_digest(token, expected)

    @router.post("/approve/{decision_id}")
    async def approve(decision_id: str, t: str | None = None) -> JSONResponse:
        if not _close_action_authorized(decision_id, t):
            return JSONResponse({"status": "unauthorized", "ok": False}, status_code=401)
        result = await deps.approval_gate.approve(decision_id)
        return JSONResponse(
            {"status": result.status, "detail": result.detail, "ok": result.ok},
            status_code=200 if result.ok else 409,
        )

    @router.post("/reject/{decision_id}")
    async def reject(decision_id: str, t: str | None = None) -> JSONResponse:
        if not _close_action_authorized(decision_id, t):
            return JSONResponse({"status": "unauthorized", "ok": False}, status_code=401)
        result = await deps.approval_gate.reject(decision_id)
        return JSONResponse(
            {"status": result.status, "detail": result.detail, "ok": result.ok},
            status_code=200 if result.ok else 409,
        )

    @router.post("/pause", dependencies=[Depends(require_auth)])
    async def pause(reason: str = "manual", token: str | None = None) -> JSONResponse:
        if not _control_authorized(token):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        deps.killswitch.pause(reason)
        return JSONResponse({"status": "paused", "reason": reason})

    @router.post("/resume", dependencies=[Depends(require_auth)])
    async def resume(reason: str = "manual", token: str | None = None) -> JSONResponse:
        if not _control_authorized(token):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        deps.killswitch.resume(reason)
        return JSONResponse({"status": "resumed", "reason": reason})

    @router.post("/test-notify", dependencies=[Depends(require_auth)])
    async def test_notify() -> JSONResponse:
        """Send a test push through the bot's own notifier — verifies phone delivery end-to-end."""
        if deps.notifier is None:
            return JSONResponse({"ok": False, "error": "no notifier configured"}, status_code=400)
        try:
            await deps.notifier.send(
                "AgenticRobinhood test",
                "Notifications are working. You'll get pings like this when the bot acts, "
                "plus a daily digest and a weekly report.",
            )
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return JSONResponse({"ok": True, "sent": True})

    @router.post("/test-ai", dependencies=[Depends(require_auth)])
    async def test_ai(token: str | None = None) -> JSONResponse:
        """Verify the AI reviewer end-to-end: build a client from the CURRENT env and make one real
        Opus call on a synthetic candidate. Confirms ANTHROPIC_API_KEY loads + the model responds."""
        if not _control_authorized(token):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        from ..ai.client import build_reviewer_client
        from ..ai.reviewer import AIReviewer

        cfg = deps.settings.ai
        client = build_reviewer_client(cfg)
        if client is None:
            return JSONResponse(
                {"ok": False, "enabled": cfg.enabled,
                 "error": "AI client unavailable — check ai.enabled and that ANTHROPIC_API_KEY is "
                          "set in the environment (a redeploy is needed after adding it)."},
                status_code=200,
            )
        result = await AIReviewer(cfg, client).selftest()
        return JSONResponse(result, status_code=200)

    def _entry_action_authorized(decision_id: str, token: str | None) -> bool:
        # Trade-executing endpoint: require the per-decision HMAC token (refuse if unset/mismatch).
        from ..config import entry_action_token
        expected = entry_action_token(decision_id)
        return bool(expected) and bool(token) and hmac.compare_digest(token, expected)

    @router.post("/approve-entry/{decision_id}")
    async def approve_entry(decision_id: str, t: str | None = None) -> JSONResponse:
        """One-tap approve for an entry the weekly-premium throttle held back.

        Authorized by a per-decision token (?t=), not the guessable/leakable decision id alone,
        because approving places a REAL order. No CONTROL_TOKEN configured -> refuse."""
        if not _entry_action_authorized(decision_id, t):
            return JSONResponse({"ok": False, "status": "unauthorized"}, status_code=401)
        if deps.scanner is None:
            return JSONResponse({"ok": False, "status": "no_scanner"}, status_code=503)
        result = await deps.scanner.approve_parked_entry(decision_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @router.post("/reject-entry/{decision_id}")
    async def reject_entry(decision_id: str, t: str | None = None) -> JSONResponse:
        if not _entry_action_authorized(decision_id, t):
            return JSONResponse({"ok": False, "status": "unauthorized"}, status_code=401)
        if deps.scanner is None:
            return JSONResponse({"ok": False, "status": "no_scanner"}, status_code=503)
        result = await deps.scanner.reject_parked_entry(decision_id)
        return JSONResponse(result, status_code=200 if result.get("ok") else 409)

    @router.post("/preview-weekly", dependencies=[Depends(require_auth)])
    async def preview_weekly() -> JSONResponse:
        """Render the exact weekly report the Friday push would send (trailing 7-day window +
        cumulative + AI narrative) WITHOUT sending it — for previewing on demand."""
        from ..services.reporting import render_weekly

        title, body = await render_weekly(
            deps.settings, deps.positions, deps.orders, deps.decisions)
        return JSONResponse({"title": title, "body": body})

    @router.post("/purge-stale", dependencies=[Depends(require_auth)])
    async def purge_stale(token: str | None = None) -> JSONResponse:
        """One-time cleanup of P&L noise (reconcile-wipe / re-entry-overwrite artifacts + demo
        seeds). Safe + idempotent: only removes non-open rows with no real outcome."""
        if not _control_authorized(token):
            return JSONResponse({"status": "unauthorized"}, status_code=401)
        pos = deps.positions.purge_stale()
        jrn = deps.trade_journal.purge_incomplete() if deps.trade_journal is not None else 0
        deps.audit.record(
            AuditEventType.RECONCILE,
            {"purge_stale": True, "positions_removed": pos, "journal_removed": jrn},
            source="control",
        )
        return JSONResponse({"ok": True, "purged_positions": pos, "purged_journal": jrn})

    @router.post("/heal-decision/{decision_id}", dependencies=[Depends(require_auth)])
    async def heal_decision(
        decision_id: str, status: str = "DONE", token: str | None = None
    ) -> JSONResponse:
        """Admin override for an entry decision's status. For records the reconcile self-heal
        can't reach — a false FAILED whose position already closed before the heal shipped
        (e.g. ONDS260731P00007500). Flips the DB status only; never places an order. Auth: Basic
        Auth + CONTROL_TOKEN (no per-decision token needed since nothing executes)."""
        if not _control_authorized(token):
            return JSONResponse({"ok": False, "status": "unauthorized"}, status_code=401)
        if deps.entry_decisions is None:
            return JSONResponse({"ok": False, "error": "no entry_decisions store"}, status_code=503)
        try:
            from ..domain.enums import DecisionStatus
            target = DecisionStatus(status.upper())
        except ValueError:
            return JSONResponse(
                {"ok": False, "error": f"invalid status {status!r}",
                 "valid": [s.value for s in DecisionStatus]},
                status_code=400,
            )
        d = deps.entry_decisions.get(decision_id)
        if d is None:
            return JSONResponse({"ok": False, "status": "not_found"}, status_code=404)
        old = d.status.value
        deps.entry_decisions.set_status(decision_id, target)
        deps.audit.record(
            AuditEventType.DECISION,
            {"heal": True, "occ": d.occ_symbol, "from": old, "to": target.value},
            source="control", decision_id=decision_id,
        )
        return JSONResponse({"ok": True, "occ": d.occ_symbol, "from": old, "to": target.value})

    @router.get("/mcp-tools", dependencies=[Depends(require_auth)])
    async def mcp_tools(tool: str | None = None) -> JSONResponse:
        """Diagnostic: the tool list the Robinhood MCP exposed at last connect + resolved roles.
        Pass ``?tool=a,b`` to get those tools' full description + input schema (to plan wiring).
        Read-only — surfaces which RH capabilities are available (technicals, options historicals,
        earnings, financials, Level II, tax lots…)."""
        broker = getattr(deps.scanner, "broker", None) if deps.scanner is not None else None
        if broker is None:
            return JSONResponse({"ok": False, "error": "no broker on scanner"}, status_code=503)
        tools = list(getattr(broker, "_tools", None) or [])
        defs = getattr(broker, "_tool_defs", None) or {}
        if tool:
            want = [t.strip() for t in tool.split(",") if t.strip()]
            return JSONResponse({
                "ok": True,
                "schemas": {n: defs.get(n, {"error": "not found"}) for n in want},
            })
        return JSONResponse({
            "ok": True,
            "broker": broker.capabilities().name,
            "note": "tools as of the last broker connect (startup)",
            "tool_count": len(tools),
            "tools": sorted(tools),
            "resolved_roles": getattr(broker, "_roles", None) or {},
        })

    @router.get("/status", dependencies=[Depends(require_auth)])
    async def status() -> dict:
        return {
            "paused": deps.killswitch.is_paused(),
            "reason": deps.killswitch.reason(),
            "mode": deps.settings.mode,
            "live_armed": deps.settings.is_live,
        }

    @router.get("/broker-status", dependencies=[Depends(require_auth)])
    async def broker_status() -> JSONResponse:
        """Definitive read of the ACTIVE broker: name (paper vs robinhood_mcp), whether it's the
        real account, and a live buying-power / open-position read (which exercises the RH
        connection — a read_error here means OAuth/connection is broken)."""
        broker = getattr(deps.scanner, "broker", None) if deps.scanner is not None else None
        if broker is None:
            return JSONResponse({"ok": False, "error": "no broker on scanner"}, status_code=503)
        caps = broker.capabilities()
        out: dict = {
            "ok": True,
            "broker": caps.name,
            "is_paper": caps.is_paper,
            "supports_options_orders": caps.supports_options_orders,
            "notes": getattr(caps, "notes", None),
        }
        try:
            out["buying_power"] = await broker.get_buying_power()
            out["open_positions"] = len(await broker.get_open_positions())
        except Exception as exc:  # noqa: BLE001 — surface the live-read failure verbatim
            out["read_error"] = f"{type(exc).__name__}: {exc}"
        return JSONResponse(out)

    return router
