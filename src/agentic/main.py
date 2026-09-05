"""Entrypoint: build the app graph and run the monitor + reconcile loops.

Phase 0: read-only monitoring in paper mode. The FastAPI webhook/control server is
added in Phase 3; for now this runs the two async background loops until interrupted.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from .brokers.factory import broker_degraded, build_broker
from .config import Settings, load_config
from .logging_setup import setup_logging
from .marketdata.alpaca_md import AlpacaMarketData
from .marketdata.base import MarketDataProvider, PaperMarketData
from .marketdata.earnings import build_earnings_provider
from .marketdata.news import build_news_provider
from .notify.factory import build_notifier
from .rules.engine import RulesEngine
from .services.approval import ApprovalGate
from .services.executor import OrderExecutor
from .services.killswitch import KillSwitch
from .services.monitor import MonitorLoop
from .services.reconcile import ReconcileLoop
from .services.reporting import ReportingLoop
from .services.roll import RollManager
from .services.scanner import OpportunityScanner
from .services.signal_processor import SignalProcessor
from .ai.client import build_reviewer_client
from .ai.reviewer import AIReviewer
from .store.ai_reviews import AIReviewStore
from .store.audit import AuditStore
from .store.db import Database
from .store.decisions import DecisionStore
from .store.entry_candidates import EntryCandidateStore
from .store.entry_decisions import EntryDecisionStore
from .store.orders import OrderStore
from .store.positions import PositionStore
from .store.signals import SignalStore
from .store.trade_journal import TradeJournalStore
from .store.news import NewsStore
from .store.tv_indicators import TVIndicatorStore

log = logging.getLogger("agentic.main")


def build_market_data(settings: Settings) -> MarketDataProvider:
    if settings.market_data == "alpaca":
        return AlpacaMarketData(feed=settings.entry.feed)
    return PaperMarketData()


def build_web_server(settings, signals, killswitch, approval_gate, audit,
                     positions, orders, decisions, entry_decisions, scanner, trade_journal,
                     tv_indicators=None, ai_reviews=None, notifier=None, entry_candidates=None,
                     news=None):
    """Build a uvicorn Server for the control/webhook/dashboard API, or None if disabled."""
    if not settings.web.enabled:
        log.info("Web API disabled (web.enabled=false).")
        return None
    try:
        import uvicorn

        from .config import get_secret
        from .web.app import WebDeps, create_app
    except ImportError:
        log.warning("Web API requested but the 'web' extra is not installed; skipping.")
        return None

    if not get_secret("DASHBOARD_PASSWORD"):
        log.warning(
            "SECURITY: web API enabled but DASHBOARD_PASSWORD is not set — /dashboard, "
            "/api/*, and /control status/pause/resume are UNAUTHENTICATED. Set "
            "DASHBOARD_PASSWORD (and DASHBOARD_USER) to lock them down before public exposure."
        )

    deps = WebDeps(
        settings=settings, signals=signals, killswitch=killswitch,
        approval_gate=approval_gate, audit=audit,
        positions=positions, orders=orders, decisions=decisions,
        entry_decisions=entry_decisions, scanner=scanner, trade_journal=trade_journal,
        tv_indicators=tv_indicators, ai_reviews=ai_reviews, notifier=notifier,
        entry_candidates=entry_candidates, news=news,
    )
    app = create_app(deps)
    config = uvicorn.Config(
        app, host=settings.web.host, port=settings.web.port, log_level="info"
    )
    return uvicorn.Server(config)


async def main_async(config_path: str | None = None) -> None:
    setup_logging()
    settings = load_config(config_path)

    if settings.mode == "live" and not settings.i_understand_live_trading:
        log.warning(
            "mode=live but i_understand_live_trading is false — running read-only. "
            "Set i_understand_live_trading: true to arm live trading."
        )

    db = Database(settings.db_path)
    audit = AuditStore(db)
    positions = PositionStore(db)
    decisions = DecisionStore(db)
    entry_decisions = EntryDecisionStore(db)
    entry_candidates = EntryCandidateStore(db)
    trade_journal = TradeJournalStore(db)
    tv_indicators = TVIndicatorStore(db)
    news = NewsStore(db)
    ai_reviews = AIReviewStore(db)
    ai_reviewer = AIReviewer(settings.ai, build_reviewer_client(settings.ai))
    orders = OrderStore(db)
    signals = SignalStore(db)
    killswitch = KillSwitch(db, audit, auto_trip_threshold=settings.auto_trip_after_errors)

    broker = await build_broker(settings)
    market_data = build_market_data(settings)
    notifier = build_notifier(settings)
    rules_engine = RulesEngine.from_configs(settings.rules)
    executor = OrderExecutor(
        settings, broker, market_data, positions, orders, decisions, audit, killswitch,
        notifier=notifier, entry_decisions=entry_decisions, trade_journal=trade_journal,
    )
    scanner = OpportunityScanner(
        settings, broker, market_data, entry_decisions, executor, audit, killswitch,
        trade_journal=trade_journal, ai_reviewer=ai_reviewer, tv_indicators=tv_indicators,
        ai_reviews=ai_reviews, earnings=build_earnings_provider(settings, broker),
        entry_candidates=entry_candidates,
        news_provider=build_news_provider(settings), news=news,
    )
    approval_gate = ApprovalGate(
        settings, decisions, positions, executor, audit, notifier=notifier
    )
    signal_processor = SignalProcessor(
        settings, signals, positions, decisions, executor, approval_gate, audit
    )

    caps = broker.capabilities()
    log.info(
        "Starting AgenticRobinhood: mode=%s live_armed=%s broker=%s options=%s data=%s",
        settings.mode, settings.is_live, caps.name, caps.supports_options_orders,
        settings.market_data,
    )
    # Loud alarm for the "looks live but isn't" state: live-armed, but the broker fell back to
    # paper (RH connect failed). The bot is not managing the real account — page the operator.
    if broker_degraded(settings, broker):
        warn = (f"mode=live but the active broker is the PAPER simulator (configured "
                f"'{settings.broker}' failed to connect). The bot is NOT trading or managing your "
                f"real Robinhood account. Fix the RH token (rh_login) and redeploy.")
        log.error("DEGRADED BROKER: %s", warn)
        try:
            await notifier.send("Bot on SIMULATOR - not live!", warn, priority="high")
        except Exception as exc:  # noqa: BLE001 — alert must not block startup
            log.warning("degraded-broker alert failed to send: %s", exc)

    roll_manager = RollManager(
        settings, broker, market_data, executor, decisions, entry_decisions, audit,
        notifier=notifier,
    )
    monitor = MonitorLoop(
        settings, broker, market_data, positions, audit, killswitch,
        rules_engine=rules_engine, decisions=decisions, notifier=notifier,
        executor=executor, signal_processor=signal_processor, approval_gate=approval_gate,
        roll_manager=roll_manager,
    )
    reconcile = ReconcileLoop(
        settings, broker, positions, audit, orders=orders, killswitch=killswitch,
        notifier=notifier, trade_journal=trade_journal, entry_decisions=entry_decisions,
    )

    web_server = build_web_server(
        settings, signals, killswitch, approval_gate, audit,
        positions, orders, decisions, entry_decisions, scanner, trade_journal,
        tv_indicators=tv_indicators, ai_reviews=ai_reviews, notifier=notifier,
        entry_candidates=entry_candidates, news=news,
    )

    reporting = ReportingLoop(
        settings, positions, orders, decisions, notifier,
        scanner=scanner, trade_journal=trade_journal, tv_indicators=tv_indicators,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(*_a) -> None:
        log.info("Shutdown requested.")
        stop_event.set()
        monitor.stop()
        reconcile.stop()
        scanner.stop()
        reporting.stop()
        if web_server is not None:
            web_server.should_exit = True

    # Signal handlers (POSIX). On Windows, KeyboardInterrupt handles Ctrl+C.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, AttributeError):
            pass

    tasks = [asyncio.create_task(monitor.run()), asyncio.create_task(reconcile.run())]
    tasks.append(asyncio.create_task(reporting.run()))
    if settings.entry.enabled:
        log.info("Entry scanner ENABLED (watchlist=%d, feed=%s).",
                 len(settings.entry.watchlist), settings.entry.feed)
        tasks.append(asyncio.create_task(scanner.run()))
    if web_server is not None:
        log.info("Control/webhook API on http://%s:%d", settings.web.host, settings.web.port)
        tasks.append(asyncio.create_task(web_server.serve()))
    try:
        await stop_event.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        _request_stop()
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        db.close()
        log.info("Stopped.")


def run() -> None:
    """Console-script entrypoint (``agentic``)."""
    import sys

    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(main_async(config_path))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
