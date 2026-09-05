"""FastAPI app factory.

``create_app(deps)`` builds the application from a ``WebDeps`` bundle so the same wiring
is used by ``main.py`` (live) and tests (via ``fastapi.testclient``). fastapi is an
optional dependency (the ``web`` extra); importing this module requires it, but the core
service runs without it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from ..config import Settings
from ..services.approval import ApprovalGate
from ..services.killswitch import KillSwitch
from ..store.audit import AuditStore
from ..store.decisions import DecisionStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.orders import OrderStore
from ..store.positions import PositionStore
from ..store.signals import SignalStore
from ..store.trade_journal import TradeJournalStore
from ..store.tv_indicators import TVIndicatorStore
from .control import make_control_router
from .dashboard import make_dashboard_router
from .settings import make_settings_router
from .webhook import make_webhook_router


@dataclass
class WebDeps:
    settings: Settings
    signals: SignalStore
    killswitch: KillSwitch
    approval_gate: ApprovalGate
    audit: AuditStore
    positions: PositionStore
    orders: OrderStore
    decisions: DecisionStore
    entry_decisions: EntryDecisionStore | None = None
    scanner: Any | None = None   # OpportunityScanner (loose type to avoid an import cycle)
    trade_journal: TradeJournalStore | None = None
    tv_indicators: TVIndicatorStore | None = None
    ai_reviews: Any | None = None   # store.ai_reviews.AIReviewStore
    notifier: Any | None = None     # notify.base.Notifier (for /control/test-notify)
    entry_candidates: Any | None = None  # store.entry_candidates.EntryCandidateStore
    news: Any | None = None         # store.news.NewsStore (advisory news/catalyst channel)


def create_app(deps: WebDeps) -> FastAPI:
    app = FastAPI(title="AgenticRobinhood", version="0.1.0")

    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "mode": deps.settings.mode,
            "live_armed": deps.settings.is_live,
            "paused": deps.killswitch.is_paused(),
        }

    app.include_router(make_webhook_router(deps))
    app.include_router(make_control_router(deps))
    app.include_router(make_settings_router(deps))
    app.include_router(make_dashboard_router(deps))
    return app
