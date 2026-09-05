"""TradingView webhook endpoint.

POST /webhook/tradingview with JSON ``{token, action, symbol?, occ?, alert_id?}``.

The handler is intentionally thin: validate the shared-secret token (constant-time),
build a dedup key (the alert id, or a sha256 of the exact body bytes), and enqueue a
Signal. The monitor's SignalProcessor does the matching/closing on its next cycle. This
decouples the public HTTP surface from execution and makes duplicate TradingView
deliveries idempotent via the UNIQUE dedup index.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..config import get_secret
from ..domain.enums import AuditEventType, SignalStatus
from ..domain.models import Signal, utcnow

if TYPE_CHECKING:
    from .app import WebDeps

log = logging.getLogger("agentic.web.webhook")


def make_webhook_router(deps: "WebDeps") -> APIRouter:
    router = APIRouter()

    @router.post("/webhook/tradingview")
    async def tradingview(request: Request) -> JSONResponse:
        body = await request.body()
        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JSONResponse({"status": "bad_request", "detail": "invalid JSON"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"status": "bad_request", "detail": "expected object"}, status_code=400)

        expected = get_secret("TRADINGVIEW_WEBHOOK_TOKEN")
        # Token may arrive in the body, an X-Webhook-Token header, or a ?t= URL query param.
        # The URL form lets TradingView alerts keep the secret out of the (loggable) message body.
        provided = str(
            payload.get("token") or request.headers.get("X-Webhook-Token")
            or request.query_params.get("t") or ""
        )
        token_ok = bool(expected) and hmac.compare_digest(provided, expected)
        if not token_ok:
            deps.audit.record(
                AuditEventType.SIGNAL, {"rejected": "bad_token"}, source="webhook"
            )
            return JSONResponse({"status": "unauthorized"}, status_code=401)

        action = str(payload.get("action") or "close")

        # Indicator alerts are an ENTRY-CONTEXT channel, not a close signal: upsert the latest
        # snapshot per symbol for the AI reviewer to read. They bypass the Signal queue/dedup.
        if action == "indicator":
            symbol = str(payload.get("symbol") or payload.get("underlying") or "").strip().upper()
            if not symbol:
                return JSONResponse(
                    {"status": "bad_request", "detail": "indicator alert needs a symbol"},
                    status_code=400,
                )
            if deps.tv_indicators is not None:
                # Never persist/echo the shared secret with the indicator data.
                deps.tv_indicators.upsert(
                    symbol, {k: v for k, v in payload.items() if k != "token"})
            deps.audit.record(
                AuditEventType.SIGNAL, {"indicator": True, "symbol": symbol}, source="webhook"
            )
            return JSONResponse({"status": "indicator_stored", "symbol": symbol}, status_code=202)

        # News/catalyst push (e.g. curated X feeds via a relay script): store per symbol as context.
        # Advisory only — never queued as a close signal, never a trade-picker.
        if action == "news":
            symbol = str(payload.get("symbol") or payload.get("underlying") or "").strip().upper()
            headline = str(payload.get("headline") or payload.get("title") or "").strip()
            if not symbol or not headline:
                return JSONResponse(
                    {"status": "bad_request", "detail": "news alert needs symbol + headline"},
                    status_code=400,
                )
            if deps.news is not None:
                src = str(payload.get("source") or "webhook")
                nid = payload.get("id") or hashlib.sha256(
                    f"{src}:{symbol}:{headline}".encode()).hexdigest()[:16]
                deps.news.add(
                    symbol=symbol, headline=headline, dedup_key=f"{src}:{nid}:{symbol}",
                    source=src, url=payload.get("url"), created_at=payload.get("created_at"),
                )
            deps.audit.record(
                AuditEventType.SIGNAL, {"news": True, "symbol": symbol, "source":
                                        payload.get("source")}, source="webhook"
            )
            return JSONResponse({"status": "news_stored", "symbol": symbol}, status_code=202)

        alert_id = payload.get("alert_id")
        dedup_key = str(alert_id) if alert_id else hashlib.sha256(body).hexdigest()
        ttl = deps.settings.web.signal_ttl_seconds
        signal = Signal(
            raw=payload,
            dedup_key=dedup_key,
            token_ok=True,
            action=action,
            status=SignalStatus.NEW,
            ttl_expires_at=utcnow() + timedelta(seconds=ttl),
        )
        is_new = deps.signals.insert_if_new(signal)
        deps.audit.record(
            AuditEventType.SIGNAL,
            {"queued": is_new, "duplicate": not is_new, "dedup_key": dedup_key,
             "action": signal.action},
            source="webhook",
        )
        return JSONResponse(
            {"status": "queued" if is_new else "duplicate", "dedup_key": dedup_key},
            status_code=202 if is_new else 200,
        )

    return router
