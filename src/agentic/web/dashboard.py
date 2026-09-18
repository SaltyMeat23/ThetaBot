"""Read-only performance dashboard: JSON APIs + one self-contained HTML page.

  GET /                -> the dashboard page
  GET /dashboard       -> the dashboard page
  GET /api/stats       -> win rate, realized/unrealized P&L, per-rule rollup
  GET /api/positions   -> open + closed positions with P&L and the rule that closed them
  GET /api/decisions   -> recent close decisions with reasons (the "why", for memory tuning)
  GET /api/audit       -> recent audit events

Everything here is read-only; mutations stay in /control/*. The page polls the APIs with
vanilla JS so there is no build step and nothing to bundle.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..services.stats import compute_stats, position_rows
from .auth import require_auth
from .calc_page import CALC_PAGE

if TYPE_CHECKING:
    from .app import WebDeps


def _sector_exposure(positions, riskcfg, account_value):
    """Current short-put collateral grouped by sector — a concentration signal for the tactical read.
    Filters to open positions, then reuses risk_breaker.sector_exposure so the math never drifts."""
    from ..services.risk_breaker import sector_exposure
    open_pos = [p for p in positions
                if str(getattr(getattr(p, "status", None), "value", getattr(p, "status", None)))
                in ("OPEN", "CLOSING")]
    exp = sector_exposure(open_pos, getattr(riskcfg, "sector_map", None))
    return [{"sector": k, "collateral": round(v, 0),
             "pct_of_account": round(v / account_value * 100, 1) if account_value else None}
            for k, v in sorted(exp.items(), key=lambda x: -x[1])]


def make_dashboard_router(deps: "WebDeps") -> APIRouter:
    # All dashboard + data routes require auth (when a password is configured).
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("/api/stats")
    async def api_stats() -> dict:
        # Live mode: show only real trades (exclude leftover paper-soak history).
        positions = deps.positions.list_all()
        orders = deps.orders.list_all()
        decisions = deps.decisions.recent(1000)
        real_only = deps.settings.is_live
        # Top-level numbers are ALL-TIME (the dashboard's headline P&L). ``this_week`` re-runs the
        # same aggregation windowed to trades resolved since the most recent Monday 00:00 UTC — the
        # market week — so the card can show both without double-counting or a separate code path.
        stats = compute_stats(positions, orders, decisions, real_only=real_only)
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        wk = compute_stats(positions, orders, decisions, since=week_start, real_only=real_only)
        stats["this_week"] = {
            "since": week_start.date().isoformat(),
            "realized_pnl": wk["realized_pnl"],
            "wins": wk["wins"],
            "losses": wk["losses"],
            "resolved_count": wk["resolved_count"],
            "win_rate": wk["win_rate"],
        }
        return stats

    @router.get("/api/positions")
    async def api_positions() -> dict:
        rows = position_rows(
            deps.positions.list_all(), deps.orders.list_all(), deps.decisions.recent(1000),
            real_only=deps.settings.is_live,
        )
        return {"positions": rows}

    @router.get("/api/decisions")
    async def api_decisions(limit: int = 100) -> dict:
        ds = deps.decisions.recent(limit)
        return {"decisions": [{
            "created_at": d.created_at.isoformat(),
            "rule_name": d.rule_name,
            "rule_type": d.rule_type.value,
            "reason": d.reason,
            "requires_approval": d.requires_approval,
            "status": d.status.value,
            "position_id": d.position_id,
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
        } for d in ds]}

    @router.get("/api/audit")
    async def api_audit(limit: int = 50) -> dict:
        return {"events": deps.audit.recent(limit)}

    @router.get("/api/candidates")
    async def api_candidates() -> dict:
        sc = deps.scanner
        cands = list(getattr(sc, "last_candidates", []) or []) if sc else []
        ctx = getattr(sc, "last_context", {}) if sc else {}
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        return {
            "scanned_at": scanned.isoformat() if scanned else None,
            "candidates": [{
                "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
                "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta,
                "iv": c.iv, "premium": c.premium, "annualized_ror": c.annualized_ror,
                "theta": c.theta, "gamma": c.gamma, "theta_efficiency": c.theta_efficiency,
                "open_interest": c.open_interest, "volume": c.volume,
                "iv_rank": ctx[c.underlying].iv_rank if c.underlying in ctx else None,
            } for c in cands[:100]],
        }

    @router.get("/api/candidate-log")
    async def api_candidate_log(limit: int = 200) -> dict:
        """Historical scan dispositions: every screened candidate + why it was approved/rejected.
        The negative-example dataset for refining entry logic."""
        store = deps.entry_candidates
        return {"candidates": store.recent(limit) if store is not None else []}

    @router.get("/api/option-oi")
    async def api_option_oi(symbol: str, dte_max: int = 75) -> dict:
        """Read-only option-chain OPEN INTEREST + volume (calls AND puts) for one underlying.

        Sourced via the LIVE Robinhood MCP — Alpaca's option snapshots do not carry open interest.
        Reuses the bot's OWN broker instance so the OAuth session lock is shared (no separate session
        that could race a token refresh). Fail-open: returns available=False when RH isn't the broker
        or the fetch fails, so it never disturbs trading."""
        sym = symbol.upper()
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc else None
        if broker is None or not hasattr(broker, "_call_tool"):
            return {"symbol": sym, "available": False,
                    "reason": "Robinhood MCP broker not active (Alpaca carries no OI)."}
        try:
            from datetime import date as _date

            from ..marketdata.robinhood_md import RobinhoodMarketData
            md = RobinhoodMarketData(broker, dte_window_days=dte_max)
            chain = await md.get_chain(sym)
        except Exception as exc:  # noqa: BLE001 — advisory read; never crash the dashboard
            return {"symbol": sym, "available": False, "reason": f"chain fetch failed: {exc}"}
        today = _date.today()
        rows, call_oi, put_oi, call_vol, put_vol = [], 0, 0, 0, 0
        for c in chain:
            ot = str(getattr(c.option_type, "value", c.option_type)).lower()
            oi, vol = (c.open_interest or 0), (c.volume or 0)
            if ot == "call":
                call_oi += oi; call_vol += vol
            elif ot == "put":
                put_oi += oi; put_vol += vol
            rows.append({"type": ot, "strike": c.strike, "expiration": c.expiration.isoformat(),
                         "dte": (c.expiration - today).days, "delta": c.delta, "iv": c.iv,
                         "open_interest": c.open_interest, "volume": c.volume, "mark": c.mark})
        rows.sort(key=lambda r: (r["open_interest"] or 0), reverse=True)
        return {"symbol": sym, "available": True, "count": len(rows),
                "summary": {"call_oi": call_oi, "put_oi": put_oi,
                            "call_put_oi_ratio": round(call_oi / put_oi, 2) if put_oi else None,
                            "call_vol": call_vol, "put_vol": put_vol,
                            "call_put_vol_ratio": round(call_vol / put_vol, 2) if put_vol else None},
                "contracts": rows[:150]}

    @router.post("/api/screen")
    async def api_screen(body: dict = Body(default={})) -> dict:
        """On-demand CSP screener over an arbitrary universe with adjustable filters. Body:
        {symbols?:[..], delta_min?, delta_max?, dte_min?, dte_max?, min_annualized_yield?,
         min_open_interest?, min_volume?, max_spread_pct?, limit?}. Symbols default to the
        watchlist; filters default to the configured entry criteria. Read-only."""
        sc = deps.scanner
        md = getattr(sc, "market_data", None) if sc is not None else None
        if md is None:
            return {"ok": False, "error": "market data unavailable", "candidates": []}
        from ..services.screening import screen_universe
        syms = body.get("symbols") or list(deps.settings.entry.watchlist)
        syms = [str(s).upper().strip() for s in syms if str(s).strip()][:30]
        over = {k: body[k] for k in (
            "delta_min", "delta_max", "dte_min", "dte_max", "min_annualized_yield",
            "min_open_interest", "min_volume", "max_spread_pct") if body.get(k) is not None}
        try:
            crit = deps.settings.entry.criteria.model_copy(update=over)
        except Exception as exc:  # noqa: BLE001 — bad filter value -> 400-ish, don't crash
            return {"ok": False, "error": f"invalid filter: {exc}", "candidates": []}
        limit = min(int(body.get("limit") or 50), 200)
        cands = await screen_universe(md, syms, crit, limit=limit)
        return {"ok": True, "symbols": syms, "count": len(cands), "candidates": [{
            "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
            "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta, "iv": c.iv,
            "premium": c.premium, "annualized_ror": c.annualized_ror, "theta": c.theta,
            "theta_efficiency": c.theta_efficiency, "open_interest": c.open_interest,
            "volume": c.volume, "break_even": c.break_even,
        } for c in cands]}

    @router.post("/api/opportunities")
    async def api_opportunities(body: dict = Body(default={})) -> dict:
        """Curated opportunity scan: discover Alpaca's most-active names in a price band, screen
        each for CSPs, rank by theta-efficiency. Body: {price_min?, price_max?, universe_size?,
        max_screen?, limit?, + the same filter overrides as /api/screen}. Read-only."""
        sc = deps.scanner
        md = getattr(sc, "market_data", None) if sc is not None else None
        if md is None:
            return {"ok": False, "error": "market data unavailable", "candidates": []}
        from ..services.screening import opportunity_scan
        over = {k: body[k] for k in (
            "delta_min", "delta_max", "dte_min", "dte_max", "min_annualized_yield",
            "min_open_interest", "min_volume", "max_spread_pct") if body.get(k) is not None}
        try:
            crit = deps.settings.entry.criteria.model_copy(update=over)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"invalid filter: {exc}", "candidates": []}
        res = await opportunity_scan(
            md, crit,
            price_min=float(body.get("price_min", 7)), price_max=float(body.get("price_max", 20)),
            universe_size=min(int(body.get("universe_size") or 100), 300),
            max_screen=min(int(body.get("max_screen") or 25), 40),
            limit=min(int(body.get("limit") or 60), 200))
        prices = res["prices"]
        return {"ok": True, "universe": res["universe"], "scanned": res["scanned"],
                "count": len(res["candidates"]), "candidates": [{
                    "underlying": c.underlying, "price": prices.get(c.underlying),
                    "occ_symbol": c.occ_symbol, "strike": c.strike, "dte": c.dte, "delta": c.delta,
                    "iv": c.iv, "premium": c.premium, "annualized_ror": c.annualized_ror,
                    "theta": c.theta, "theta_efficiency": c.theta_efficiency,
                    "open_interest": c.open_interest, "volume": c.volume, "break_even": c.break_even,
                } for c in res["candidates"]]}

    @router.get("/api/technicals")
    async def api_technicals() -> dict:
        """Per-watchlist-symbol technicals from the last scan (rsi, sma50/200, above_sma200, atr,
        iv_rank, drawdown, days_to_earnings) — the decision inputs, for transparency + gate setup."""
        sc = deps.scanner
        ctx = getattr(sc, "last_context", {}) if sc is not None else {}
        scanned = getattr(sc, "last_scan_at", None) if sc is not None else None
        return {
            "scanned_at": scanned.isoformat() if scanned else None,
            "symbols": {sym: c.as_dict() for sym, c in ctx.items()},
        }

    @router.get("/api/quality")
    async def api_quality() -> dict:
        """Per-watchlist-symbol company-quality readout from the last scan — the 0-100 score plus
        the raw fundamentals behind it (margins, FCF, revenue growth, insider buys, sector).
        INFORMATIONAL ONLY: this is a 'is this name actually profitable/decent?' view and does not
        affect which trades are placed (see entry.quality_scoring). Empty unless scoring is enabled."""
        sc = deps.scanner
        q = getattr(sc, "last_quality", {}) if sc is not None else {}
        scanned = getattr(sc, "last_scan_at", None) if sc is not None else None
        return {
            "enabled": bool(getattr(deps.settings.entry, "quality_scoring", False)),
            "scanned_at": scanned.isoformat() if scanned else None,
            "symbols": q,
        }

    @router.get("/api/scan-status")
    async def api_scan_status() -> dict:
        from ..services.market_hours import is_market_hours
        sc = deps.scanner
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        return {
            "enabled": deps.settings.entry.enabled,
            "market_open": is_market_hours(),
            "paused": deps.killswitch.is_paused(),
            "last_scan_at": scanned.isoformat() if scanned else None,
            "watchlist": len(deps.settings.entry.watchlist),
            "feed": deps.settings.entry.feed,
            "last_skips": list(getattr(sc, "last_skips", []) or []) if sc else [],
            "last_error": getattr(sc, "last_error", None) if sc else None,
        }

    @router.get("/api/tv-indicators")
    async def api_tv_indicators() -> dict:
        store = deps.tv_indicators
        return {"indicators": store.recent(50) if store is not None else []}

    @router.get("/api/tv-health")
    async def api_tv_health() -> dict:
        from ..services.tv_health import build_tv_health
        return build_tv_health(
            deps.tv_indicators, deps.settings.entry.watchlist,
            deps.settings.ai.tv_indicator_max_age_seconds,
        )

    @router.get("/api/setups")
    async def api_setups() -> dict:
        """Technical setup detections per watchlist name (deterministic daily-bar reads: washout /
        coiling / breakout / breakdown / support tests). Served from the scanner's cache -- no
        market-data calls. Descriptive; the opt-in setup gates and prefer_setups tilt are what
        feed trading."""
        from ..services.setups_view import build_setups_view
        sc = deps.scanner
        return build_setups_view(
            getattr(sc, "last_setups", {}) if sc else {},
            getattr(sc, "last_context", {}) if sc else {},
            getattr(sc, "last_skips", []) if sc else [],
            deps.settings.entry, getattr(sc, "last_scan_at", None) if sc else None,
        )

    @router.get("/api/risk-profile")
    async def api_risk_profile() -> dict:
        """Per-name strike-survival risk profile (computed daily by the scanner from ~1y of bars) and
        the tightening-only per-ticker cushion suggestions derived from it. Read-only."""
        from ..entry.setups import PUT_SELLER_AVOID_PRESET
        from ..services.risk_profile import propose_ticker_cushions
        sc = deps.scanner
        profiles = dict(getattr(sc, "last_risk_profile", {}) or {}) if sc else {}
        e = deps.settings.entry
        proposals = propose_ticker_cushions(profiles, e.per_ticker, base_cushion=e.setups.profile_base_cushion)
        return {
            "config": {"base_cushion": e.setups.profile_base_cushion, "horizon": e.setups.profile_horizon,
                       "target_itm_rate": e.setups.target_itm_rate,
                       "put_seller_avoid_preset": list(PUT_SELLER_AVOID_PRESET)},
            "profiles": [profiles[s] for s in sorted(profiles)],
            "proposals": proposals,
        }

    @router.get("/api/setups/accuracy")
    async def api_setups_accuracy() -> dict:
        """Measured forward outcomes per setup label (the setup-accuracy tracker): n, 5-day hit rate,
        average 5/10-day returns, average 10-day max adverse excursion. Descriptive; small n = weak."""
        store = getattr(deps.scanner, "setup_events", None) if deps.scanner else None
        if store is None:
            return {"available": False, "rows": [], "recent": []}
        try:
            return {"available": True, "rows": store.accuracy(), "recent": store.recent(50)}
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "rows": [], "recent": [], "error": str(exc)}

    @router.get("/api/ops")
    async def api_ops() -> dict:
        """One-call operational snapshot: loops, killswitch, sync, scan health, TV freshness."""
        from datetime import datetime, timezone
        from ..domain.enums import AuditEventType
        from ..services.market_hours import is_market_hours
        from ..services.tv_health import build_tv_health

        now = datetime.now(timezone.utc)

        def lag(event_type, source):
            row = deps.audit.latest(event_type, source)
            if not row:
                return {"last_at": None, "lag_seconds": None}
            ts = datetime.fromisoformat(row["ts"])
            return {"last_at": row["ts"], "lag_seconds": round((now - ts).total_seconds(), 1)}

        recon = deps.audit.latest(AuditEventType.RECONCILE, "reconcile")
        sync = None
        if recon:
            p = recon["payload"]
            sync = {
                "broker_open": p.get("broker_open"), "store_open": p.get("store_open"),
                "in_sync": p.get("broker_open") == p.get("store_open"),
                "healed": p.get("entry_decisions_healed", []),
            }
        sc = deps.scanner
        scanned = getattr(sc, "last_scan_at", None) if sc else None
        ks = deps.killswitch
        last_err = deps.audit.latest(AuditEventType.ERROR)
        tvh = build_tv_health(
            deps.tv_indicators, deps.settings.entry.watchlist,
            deps.settings.ai.tv_indicator_max_age_seconds,
        ) if deps.tv_indicators is not None else None
        brk = getattr(sc, "broker", None) if sc is not None else None
        broker_block = None
        if brk is not None:
            from ..brokers.factory import broker_degraded
            caps = brk.capabilities()
            broker_block = {"name": caps.name, "is_paper": caps.is_paper,
                            "degraded": broker_degraded(deps.settings, brk)}
        return {
            "mode": deps.settings.mode, "live_armed": deps.settings.is_live,
            "paused": ks.is_paused(), "pause_reason": ks.reason(),
            "broker": broker_block,
            "killswitch": {
                "consecutive_errors": ks.consecutive_errors(),
                "auto_trip_after": deps.settings.auto_trip_after_errors,
            },
            "loss_breaker": getattr(sc, "last_breaker", None),
            "loops": {
                "monitor_poll": lag(AuditEventType.POLL, "monitor"),
                "reconcile": lag(AuditEventType.RECONCILE, "reconcile"),
                "scanner": lag(AuditEventType.POLL, "scanner"),
            },
            "sync": sync,
            "scan": {
                "enabled": deps.settings.entry.enabled,
                "market_open": is_market_hours(),
                "last_scan_at": scanned.isoformat() if scanned else None,
                "last_error": getattr(sc, "last_error", None) if sc else None,
                "skips": len(getattr(sc, "last_skips", []) or []) if sc else 0,
            },
            "last_error": (
                {"where": last_err["payload"].get("where"),
                 "root_causes": last_err["payload"].get("root_causes"),
                 "at": last_err["ts"]} if last_err else None
            ),
            "tv_health": tvh,
        }

    @router.get("/api/refinement-export")
    async def api_refinement_export(limit: int = 1000, format: str = "json"):
        """Joined feature->outcome table (journal x AI verdict) for offline model tuning.
        ``format=csv`` returns text/csv; default JSON."""
        from ..services.refinement import build_refinement_rows, rows_to_csv
        rows = build_refinement_rows(deps.trade_journal, deps.ai_reviews, limit) \
            if deps.trade_journal is not None else []
        if format == "csv":
            return PlainTextResponse(rows_to_csv(rows), media_type="text/csv")
        return {"count": len(rows), "rows": rows}

    @router.get("/api/news")
    async def api_news(symbol: str | None = None, limit: int = 50) -> dict:
        """Recent stored news/catalyst items (advisory channel: Alpaca pull + webhook push). Pass
        ``?symbol=`` for one name, else the newest across all names. Verifies the feed is flowing."""
        store = deps.news
        if store is None:
            return {"enabled": deps.settings.news.enabled, "items": []}
        items = (store.recent_for(symbol, limit=limit) if symbol else store.recent(limit))
        return {"enabled": deps.settings.news.enabled, "count": len(items), "items": items}

    @router.get("/api/analytics/features")
    async def api_analytics_features(limit: int = 1000) -> dict:
        """Descriptive win-rate + avg realized P&L bucketed by entry feature (ticker, kind, delta,
        DTE, IV, RSI, 200-SMA trend, regime, AI verdict, exit rule) over resolved trades. Read as
        hints — noisy until many trades accumulate. Reuses the refinement (feature->outcome) table."""
        from ..services.analytics import build_feature_analytics
        from ..services.refinement import build_refinement_rows
        rows = build_refinement_rows(deps.trade_journal, deps.ai_reviews, limit) \
            if deps.trade_journal is not None else []
        return build_feature_analytics(rows)

    @router.get("/api/accounts")
    async def api_accounts() -> dict:
        """All Robinhood accounts on the login, with balances (for the advisory calculator).
        READ-ONLY: the bot only TRADES the agentic_allowed account; the rest are advisory."""
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc is not None else None
        if broker is None or not hasattr(broker, "list_accounts"):
            return {"ok": False, "error": "broker unavailable", "accounts": []}
        try:
            raw_accts = await broker.list_accounts()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc), "accounts": []}
        out = []
        for a in raw_accts:
            num = a.get("account_number")
            if not num:
                continue
            try:
                bp = await broker.get_buying_power(num)
                av = await broker.get_account_value(num)
            except Exception:  # noqa: BLE001
                bp = av = 0.0
            out.append({
                "account_number": num,
                "type": a.get("brokerage_account_type") or a.get("type"),
                "nickname": a.get("nickname"),
                "agentic": bool(a.get("agentic_allowed")),
                "option_level": a.get("option_level"),
                "buying_power": round(bp, 2), "account_value": round(av, 2),
            })
        return {"ok": True, "accounts": out}

    @router.get("/api/account-options")
    async def api_account_options(account: str = "all") -> dict:
        """Advisory 'what could I sell' per account: covered calls above cost basis on held shares +
        cash-secured puts sized to buying power. READ-ONLY — suggests only, never places orders.
        ``?account=<num>`` for one account, ``all`` to aggregate across every account."""
        sc = deps.scanner
        broker = getattr(sc, "broker", None) if sc is not None else None
        md = getattr(sc, "market_data", None) if sc is not None else None
        if broker is None or md is None:
            return {"ok": False, "error": "broker/market data unavailable", "accounts": []}
        from ..services.account_options import _advisory_criteria, account_option_suggestions
        from ..services.screening import screen_universe
        try:  # pre-screen watchlist puts once — shared across accounts, differ only by affordability
            csp = await screen_universe(
                md, list(deps.settings.entry.watchlist),
                _advisory_criteria(deps.settings.entry.criteria), limit=60)
        except Exception:  # noqa: BLE001
            csp = []
        if account == "all":
            try:
                nums = [a.get("account_number") for a in await broker.list_accounts()
                        if a.get("account_number")]
            except Exception:  # noqa: BLE001
                nums = []
        else:
            nums = [account]
        results = []
        for num in nums:
            try:
                results.append(await account_option_suggestions(
                    broker, md, deps.settings, num, csp_candidates=csp))
            except Exception as exc:  # noqa: BLE001
                results.append({"account_number": num, "error": str(exc)})
        ok_rows = [r for r in results if "error" not in r]
        tot = lambda key: round(sum(s["weekly_dollars"] for r in ok_rows for s in r.get(key, [])), 2)
        return {"ok": True, "accounts": results,
                "total_cc_weekly": tot("covered_calls"),
                "total_csp_weekly": tot("cash_secured_puts"),
                "total_weekly_target": round(sum(r.get("weekly_target", 0) for r in ok_rows), 2)}

    @router.get("/api/regime")
    async def api_regime() -> dict:
        sc = deps.scanner
        reg = getattr(sc, "last_regime", None) if sc else None
        return {
            "enabled": deps.settings.macro.enabled,
            "hard_gate": deps.settings.macro.hard_gate,
            "skip_confirmed_downtrend": deps.settings.macro.skip_confirmed_downtrend,
            "downtrend_confirm_days": deps.settings.macro.downtrend_confirm_days,
            "regime": reg.as_dict() if reg is not None else None,
        }

    @router.get("/api/brief")
    async def api_brief() -> dict:
        """On-demand weekly tactical prep brief (markdown): market backdrop, this week's catalysts,
        per-watchlist levels + the bot's mechanical rule-based strike targets, and advisory CC/CSP
        ideas across every account. DESCRIPTIVE ONLY — not financial advice. Read-only; hits the broker
        for cross-account holdings, so it is deliberately not part of the auto-refresh poll."""
        from datetime import datetime, timezone

        from ..marketdata.econ_calendar import load_econ_calendar, upcoming_events
        from ..services.weekly_brief import build_weekly_brief

        s, sc = deps.settings, deps.scanner
        now = datetime.now(timezone.utc)
        watchlist = list(s.entry.watchlist)
        try:
            ctxs = getattr(sc, "last_context", {}) if sc else {}
            contexts = {sym: ctxs[sym].as_dict() for sym in watchlist if sym in ctxs}
            cands = list(getattr(sc, "last_candidates", []) or []) if sc else []
            candidates = [{"underlying": c.underlying, "strike": c.strike, "delta": c.delta,
                           "dte": c.dte, "premium": c.premium, "iv": c.iv,
                           "annualized_ror": c.annualized_ror, "break_even": c.break_even,
                           "theta_efficiency": c.theta_efficiency, "open_interest": c.open_interest}
                          for c in cands]

            tv_by_symbol: dict = {}
            if deps.tv_indicators is not None:
                max_age = s.ai.tv_indicator_max_age_seconds
                for sym in watchlist:
                    snap = deps.tv_indicators.get_latest(sym, max_age)
                    tv_by_symbol[sym] = (snap or {}).get("payload", {}) if snap else {}

            reg = getattr(sc, "last_regime", None) if sc else None
            regime = reg.as_dict() if reg is not None else None

            news_by_symbol: dict = {}
            news_store = getattr(deps, "news", None)
            news_cfg = getattr(s, "news", None)
            if news_store is not None and getattr(news_cfg, "enabled", False):
                for sym in watchlist:
                    try:
                        items = news_store.recent_for(
                            sym, max_age_seconds=news_cfg.max_age_hours * 3600, limit=1)
                        if items:
                            news_by_symbol[sym] = items[0].get("headline") or ""
                    except Exception:  # noqa: BLE001
                        pass

            accounts: list = []
            broker = getattr(sc, "broker", None) if sc else None
            md = getattr(sc, "market_data", None) if sc else None
            if broker is not None and md is not None:
                from ..services.account_options import _advisory_criteria, account_option_suggestions
                from ..services.screening import screen_universe
                try:
                    csp = await screen_universe(md, watchlist, _advisory_criteria(s.entry.criteria), limit=60)
                except Exception:  # noqa: BLE001
                    csp = []
                try:
                    nums = [a.get("account_number") for a in await broker.list_accounts()
                            if a.get("account_number")]
                except Exception:  # noqa: BLE001
                    nums = []
                for num in nums:
                    try:
                        accounts.append(await account_option_suggestions(broker, md, s, num, csp_candidates=csp))
                    except Exception as exc:  # noqa: BLE001
                        accounts.append({"account_number": num, "error": str(exc)})

            econ_events = upcoming_events(load_econ_calendar(), now.date(), 7)

            # Rich context for the AI tactical read — all in-process, each fail-open.
            skips = list(getattr(sc, "last_skips", []) or []) if sc else []
            try:
                from ..services.analytics import build_feature_analytics
                from ..services.refinement import build_refinement_rows
                flywheel = build_feature_analytics(
                    build_refinement_rows(deps.trade_journal, deps.ai_reviews, 1000)
                ) if deps.trade_journal is not None else None
            except Exception:  # noqa: BLE001
                flywheel = None
            try:
                pos_all = deps.positions.list_all()  # fetched once, shared by stats + exposure
            except Exception:  # noqa: BLE001
                pos_all = []
            try:
                from ..services.stats import compute_stats
                stats = compute_stats(pos_all, deps.orders.list_all(),
                                      deps.decisions.recent(1000), real_only=s.is_live)
            except Exception:  # noqa: BLE001
                stats = None
            try:
                from ..services.risk_breaker import evaluate_risk_breaker
                acct_no = getattr(s.robinhood, "account_number", None)
                acct_val = next((a.get("account_value") for a in accounts
                                 if a.get("account_number") == acct_no), None)
                if acct_val is None:
                    acct_val = sum(a.get("account_value", 0) or 0 for a in accounts if "error" not in a) or None
                lb = evaluate_risk_breaker(deps.trade_journal, s.risk, acct_val, now)
                risk = {"loss_breaker": {k: lb.get(k) for k in
                        ("tripped", "reason", "window_realized", "loss_limit", "consecutive_losses")},
                        "sector_exposure": _sector_exposure(pos_all, s.risk, acct_val)}
            except Exception:  # noqa: BLE001
                risk = None
            try:
                ai_reviews = (deps.ai_reviews.recent(8) if deps.ai_reviews is not None else []) or []
            except Exception:  # noqa: BLE001
                ai_reviews = []
            try:  # measured setup outcomes on these names (setup-accuracy tracker); advisory
                _se = getattr(sc, "setup_events", None) if sc else None
                setup_accuracy = _se.accuracy() if _se is not None else None
            except Exception:  # noqa: BLE001
                setup_accuracy = None
            risk_profiles = dict(getattr(sc, "last_risk_profile", {}) or {}) if sc else None
            try:
                tax_reserve = _reserve_payload()
            except Exception:  # noqa: BLE001
                tax_reserve = None

            # Open short options (for the management + assignment-capacity reads) and total buying
            # power across accounts. Drop paper positions in live mode.
            try:
                open_positions = [p for p in pos_all
                                  if str(getattr(getattr(p, "status", None), "value",
                                                 getattr(p, "status", None))).upper() in ("OPEN", "CLOSING")
                                  and not (s.is_live and getattr(p, "is_paper", False))]
            except Exception:  # noqa: BLE001
                open_positions = []
            total_bp = sum(a.get("buying_power", 0) or 0
                           for a in accounts if "error" not in a) or None

            ai_analysis = None  # optional Claude tactical synthesis; fail-open to deterministic brief
            try:
                from ..ai.brief_analysis import generate_brief_analysis
                from ..ai.client import build_reviewer_client
                client = build_reviewer_client(s.ai)
                if client is not None:
                    ai_analysis = await generate_brief_analysis(
                        client, watchlist=watchlist, contexts=contexts, candidates=candidates,
                        tv_by_symbol=tv_by_symbol, regime=regime, econ_events=econ_events,
                        flywheel=flywheel, skips=skips, accounts=accounts, stats=stats,
                        risk=risk, ai_reviews=ai_reviews, open_positions=open_positions,
                        total_buying_power=total_bp, setup_accuracy=setup_accuracy,
                        risk_profiles=risk_profiles)
            except Exception:  # noqa: BLE001 — AI is advisory; the brief renders without it
                ai_analysis = None

            title, body = build_weekly_brief(
                watchlist=watchlist, contexts=contexts, candidates=candidates,
                tv_by_symbol=tv_by_symbol, regime=regime, news_by_symbol=news_by_symbol,
                accounts=accounts, econ_events=econ_events, now=now, ai_analysis=ai_analysis,
                open_positions=open_positions, total_buying_power=total_bp,
                tax_reserve=tax_reserve, tier_proposals=(list(getattr(sc, "last_tier_proposals", []) or []) if sc else []))
            out = {"title": title, "body": body, "created_at": now.isoformat(),
                   "has_ai": ai_analysis is not None, "id": None}
            # Persist every generation so the brief can be re-read from a phone later without
            # regenerating (which hits the broker + the AI). Advisory: a save failure never
            # hides the brief that was just built.
            try:
                if deps.briefs is not None:
                    out["id"] = deps.briefs.save(
                        title, body, has_ai=ai_analysis is not None, created_at=now,
                        meta={"watchlist": len(watchlist), "open_positions": len(open_positions),
                              "accounts": len([a for a in accounts if "error" not in a])})
            except Exception:  # noqa: BLE001
                out["id"] = None
            return out
        except Exception as exc:  # noqa: BLE001 — a brief must never 500 the dashboard
            return {"title": "Weekly tactical brief", "body": f"Brief unavailable: {exc}",
                    "created_at": now.isoformat(), "has_ai": False, "id": None}

    @router.get("/api/briefs")
    async def api_briefs(limit: int = 30) -> dict:
        """Saved briefs, newest first (no bodies). ``latest`` carries the newest full brief so the
        Brief tab opens on it without a regeneration."""
        store = deps.briefs
        if store is None:
            return {"available": False, "briefs": [], "latest": None}
        rows = store.recent(max(1, min(int(limit), 200)))
        return {"available": True, "briefs": rows, "latest": store.latest()}

    @router.get("/api/briefs/{brief_id}")
    async def api_brief_one(brief_id: str) -> dict:
        store = deps.briefs
        row = store.get(brief_id) if store is not None else None
        if row is None:
            raise HTTPException(status_code=404, detail="brief not found")
        return row

    @router.get("/api/ai-reviews")
    async def api_ai_reviews(limit: int = 100) -> dict:
        store = deps.ai_reviews
        return {
            "enabled": deps.settings.ai.enabled,
            "mode": deps.settings.ai.mode,
            "model": deps.settings.ai.model,
            "reviews": store.recent(limit) if store is not None else [],
        }

    @router.get("/api/cc-candidates")
    async def api_cc_candidates() -> dict:
        sc = deps.scanner
        cands = list(getattr(sc, "last_cc_candidates", []) or []) if sc else []
        return {"candidates": [{
            "underlying": c.underlying, "occ_symbol": c.occ_symbol, "strike": c.strike,
            "expiration": c.expiration.isoformat(), "dte": c.dte, "delta": c.delta,
            "premium": c.premium, "annualized_ror": c.annualized_ror,
        } for c in cands[:100]]}

    @router.get("/api/holdings")
    async def api_holdings() -> dict:
        sc = deps.scanner
        holds = list(getattr(sc, "last_holdings", []) or []) if sc else []
        from ..domain.enums import Direction, OptionType
        from ..services.holdings import reserve_symbols
        reserve = reserve_symbols(deps.settings)
        clock = dict(getattr(sc, "last_cc_clock", {}) or {}) if sc else {}
        covered: dict[str, int] = {}
        for p in deps.positions.list_open():
            if p.direction is Direction.SHORT and p.option_type is OptionType.CALL:
                covered[p.underlying] = covered.get(p.underlying, 0) + p.quantity
        return {"holdings": [{
            "symbol": h.symbol, "shares": h.quantity, "average_cost": h.average_cost,
            "coverable": int(h.quantity // 100), "covered": covered.get(h.symbol, 0),
            "reserve": h.symbol.upper() in reserve, "clock": clock.get(h.symbol),
        } for h in holds]}

    def _reserve_payload() -> dict:
        """Tax-reserve state for the dashboard, digest and brief: config, held shares (walled off),
        ledger totals, recent periods, what the next sweep would do."""
        sc = deps.scanner
        loop = getattr(deps, "tax_reserve", None)
        store = getattr(deps, "tax_reserve_store", None)
        out: dict = {"config": deps.settings.tax_reserve.model_dump(),
                     "holding": (getattr(sc, "last_reserve", None) if sc else None),
                     "clock": (dict(getattr(sc, "last_cc_clock", {}) or {}) if sc else {}),
                     "status": None, "totals": None, "recent": []}
        try:
            out["status"] = loop.status() if loop is not None else None
        except Exception as exc:  # noqa: BLE001
            out["status_error"] = str(exc)
        try:
            if store is not None:
                out["totals"] = store.totals()
                out["recent"] = store.recent(12)
        except Exception as exc:  # noqa: BLE001
            out["ledger_error"] = str(exc)
        return out

    @router.get("/api/tax-reserve")
    async def api_tax_reserve() -> dict:
        return _reserve_payload()

    @router.get("/api/tiers")
    async def api_tiers() -> dict:
        """Quality names the account can now afford (proposal only; the user adds with one tap)."""
        from ..services.tiers import next_unlock
        sc = deps.scanner
        e = deps.settings.entry
        av = getattr(sc, "last_account_value", None) if sc else None
        prices = dict(getattr(sc, "_tier_prices", {}) or {}) if sc else {}
        return {"ready": list(getattr(sc, "last_tier_proposals", []) or []) if sc else [],
                "next": next_unlock(e.watchlist_tiers, e.watchlist, av, e.sizing.max_pct_per_underlying, prices),
                "account_value": av, "per_name_pct": e.sizing.max_pct_per_underlying,
                "tiers": {k: {"min_collateral": v.get("min_collateral"), "note": v.get("note", "")}
                          for k, v in (e.watchlist_tiers or {}).items()}}

    @router.get("/api/journal")
    async def api_journal(limit: int = 200) -> dict:
        store = deps.trade_journal
        rows = store.recent(limit) if store else []
        return {"trades": [{
            "entered_at": j.entered_at.isoformat(), "kind": j.kind, "underlying": j.underlying,
            "occ_symbol": j.occ_symbol, "strike": j.strike, "dte": j.dte, "delta": j.delta,
            "iv": j.iv, "premium": j.premium, "annualized_ror": j.annualized_ror,
            "status": j.status, "realized_pnl": j.realized_pnl, "days_held": j.days_held,
            "exit_reason": j.exit_reason,
        } for j in rows]}

    @router.get("/api/entry-decisions")
    async def api_entry_decisions(limit: int = 100) -> dict:
        store = deps.entry_decisions
        ds = store.recent(limit) if store else []
        return {"entries": [{
            "id": d.id,
            "created_at": d.created_at.isoformat(), "underlying": d.underlying,
            "occ_symbol": d.occ_symbol, "strike": d.strike,
            "expiration": d.expiration.isoformat(), "contracts": d.contracts,
            "premium": d.premium, "status": d.status.value, "reason": d.reason,
        } for d in ds]}

    @router.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @router.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        return _PAGE

    @router.get("/calculator", response_class=HTMLResponse)
    async def calculator() -> str:
        return CALC_PAGE

    return router


# --- self-contained page (vanilla JS, no dependencies) -----------------------------------
_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Trader Cortex — AgenticRobinhood</title>
<style>
  :root{
    --paper:#f5f7f9; --card:#ffffff; --raise:#fbfcfd; --ink:#141a21; --muted:#5b6672;
    --faint:#8b96a3; --line:#e4e8ec; --accent:#2f5d7c; --accent-soft:#e8f0f5;
    --pos:#158043; --pos-bg:#e6f3ec; --neg:#c1372d; --neg-bg:#fbe9e6;
    --warn:#a9701a; --warn-bg:#f7ecd6;
    --mono:"SFMono-Regular","Cascadia Code","JetBrains Mono",ui-monospace,Menlo,Consolas,monospace;
    --sans:-apple-system,"Segoe UI",system-ui,Roboto,Helvetica,Arial,sans-serif;
    --r:14px; --r-sm:9px; --shadow:0 1px 2px rgba(20,26,33,.04),0 6px 20px -12px rgba(20,26,33,.18);
  }
  @media (prefers-color-scheme:dark){:root{
    --paper:#0d1218; --card:#151c25; --raise:#1a222c; --ink:#e7ecf2; --muted:#94a1b0;
    --faint:#6b7885; --line:#25303b; --accent:#77b4d8; --accent-soft:#132430;
    --pos:#3ecb7e; --pos-bg:#11291d; --neg:#f0685c; --neg-bg:#2b1613;
    --warn:#e2a63c; --warn-bg:#282011; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -14px rgba(0,0,0,.6);
  }}
  :root[data-theme="light"]{
    --paper:#f5f7f9; --card:#ffffff; --raise:#fbfcfd; --ink:#141a21; --muted:#5b6672;
    --faint:#8b96a3; --line:#e4e8ec; --accent:#2f5d7c; --accent-soft:#e8f0f5;
    --pos:#158043; --pos-bg:#e6f3ec; --neg:#c1372d; --neg-bg:#fbe9e6;
    --warn:#a9701a; --warn-bg:#f7ecd6; --shadow:0 1px 2px rgba(20,26,33,.04),0 6px 20px -12px rgba(20,26,33,.18);
  }
  :root[data-theme="dark"]{
    --paper:#0d1218; --card:#151c25; --raise:#1a222c; --ink:#e7ecf2; --muted:#94a1b0;
    --faint:#6b7885; --line:#25303b; --accent:#77b4d8; --accent-soft:#132430;
    --pos:#3ecb7e; --pos-bg:#11291d; --neg:#f0685c; --neg-bg:#2b1613;
    --warn:#e2a63c; --warn-bg:#282011; --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -14px rgba(0,0,0,.6);
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--sans);line-height:1.5;
    -webkit-font-smoothing:antialiased}
  .app{padding:clamp(14px,3vw,30px)}
  .shell{max-width:1080px;margin:0 auto;display:flex;flex-direction:column;gap:16px}
  .mono{font-family:var(--mono);font-variant-numeric:tabular-nums}
  .neg{color:var(--neg)} .pos-c{color:var(--pos)} .muted{color:var(--muted)}
  .masthead{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;padding:2px 4px}
  .masthead h1{font-size:19px;font-weight:650;margin:0;letter-spacing:-.01em}
  .masthead h1 span{color:var(--faint);font-weight:500}
  .updated{font-size:12.5px;color:var(--muted);display:flex;align-items:center;gap:10px}
  .rbtn{background:var(--card);color:var(--muted);border:1px solid var(--line);border-radius:8px;
    padding:5px 11px;cursor:pointer;font:inherit;font-size:12.5px}
  .rbtn:hover{border-color:var(--accent);color:var(--ink)}
  .rbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  /* connection ribbon */
  .ribbon{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);overflow:hidden}
  .ribbon-top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 18px}
  .conn{display:inline-flex;align-items:center;gap:9px;font-weight:650;font-size:15px;padding:8px 14px;
    border-radius:999px;background:var(--pos-bg);color:var(--pos);
    border:1px solid color-mix(in srgb,var(--pos) 26%,transparent)}
  .conn.bad{background:var(--neg-bg);color:var(--neg);border-color:color-mix(in srgb,var(--neg) 30%,transparent)}
  .conn.warn{background:var(--warn-bg);color:var(--warn);border-color:color-mix(in srgb,var(--warn) 30%,transparent)}
  .conn small{font-weight:500;color:var(--muted);font-size:12.5px}
  .dot{width:9px;height:9px;border-radius:50%;background:currentColor;position:relative;flex:none}
  .dot::after{content:"";position:absolute;inset:-4px;border-radius:50%;background:currentColor;opacity:.28;animation:pulse 2.4s ease-out infinite}
  @keyframes pulse{0%{transform:scale(.6);opacity:.5}70%{transform:scale(1.8);opacity:0}100%{opacity:0}}
  @media (prefers-reduced-motion:reduce){.dot::after{animation:none}}
  .chips{display:flex;gap:7px;flex-wrap:wrap;margin-left:auto}
  .chip{font-size:12px;font-weight:600;padding:5px 10px;border-radius:999px;background:var(--raise);
    border:1px solid var(--line);color:var(--muted);display:inline-flex;align-items:center;gap:6px}
  .chip b{color:var(--ink);font-weight:650}
  .chip.good{color:var(--pos);background:var(--pos-bg);border-color:transparent}
  .chip.warn{color:var(--warn);background:var(--warn-bg);border-color:transparent}
  .ribbon-note{border-top:1px dashed var(--line);padding:9px 18px;font-size:12.5px;color:var(--muted);background:var(--raise)}
  /* layout */
  .grid{display:grid;grid-template-columns:1fr 300px;gap:16px;align-items:start}
  @media (max-width:760px){.grid{grid-template-columns:1fr}}
  .col{display:flex;flex-direction:column;gap:16px;min-width:0}
  /* tab navigation — sticky, horizontally scrollable on mobile so every view is one tap away */
  .tabs{position:sticky;top:0;z-index:30;display:flex;gap:6px;overflow-x:auto;padding:9px 2px;margin:0 -2px;
    background:color-mix(in srgb,var(--paper) 88%,transparent);backdrop-filter:blur(8px);
    border-bottom:1px solid var(--line);-webkit-overflow-scrolling:touch;scrollbar-width:none}
  .tabs::-webkit-scrollbar{display:none}
  .nav-dot{width:9px;height:9px;border-radius:50%;background:var(--faint);align-self:center;margin:0 4px 0 2px;flex:none}
  .nav-dot.good{background:var(--pos)} .nav-dot.warn{background:var(--warn)} .nav-dot.bad{background:var(--neg)}
  .tab-btn{flex:0 0 auto;background:var(--card);border:1px solid var(--line);border-radius:999px;
    padding:8px 17px;font:inherit;font-size:13.5px;font-weight:600;color:var(--muted);cursor:pointer;
    white-space:nowrap;transition:background .12s,color .12s}
  .tab-btn:hover{color:var(--ink)}
  .tab-btn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
  .tab-btn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  .tabpane[hidden]{display:none}
  .tabpane{display:flex;flex-direction:column;gap:16px}
  .card2{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
  .card-h{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 16px 0}
  .card-h h2{font-size:13px;font-weight:650;margin:0}
  .count{font-size:12px;color:var(--faint);font-weight:600}
  /* position card */
  .pos-item{padding:16px} .pos-item + .pos-item{border-top:1px solid var(--line)}
  .pos-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
  .tick{font-size:17px;font-weight:700;letter-spacing:-.01em}
  .tlogo{display:inline-flex;width:26px;height:26px;margin-right:8px;vertical-align:middle}
  .tlogo img{width:26px;height:26px;border-radius:6px;object-fit:contain;background:#fff;border:1px solid var(--line)}
  .tmono{width:26px;height:26px;border-radius:6px;place-items:center;font-size:12px;font-weight:700;color:#fff}
  .tick .kind{font-size:12px;font-weight:600;color:var(--muted);margin-left:8px}
  .pos-sub{font-size:12.5px;color:var(--muted);margin-top:3px}
  .pnl{text-align:right;flex:none}
  .pnl .v{font-size:19px;font-weight:700;font-family:var(--mono);font-variant-numeric:tabular-nums}
  .pnl .l{font-size:11px;color:var(--faint);text-transform:uppercase;letter-spacing:.06em}
  .facts{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;background:var(--line);
    border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden;margin:14px 0}
  .fact{background:var(--card);padding:9px 11px}
  .fact .k{font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em}
  .fact .v{font-size:14px;font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums;margin-top:2px}
  .gauge{margin-top:6px} .gauge-lab{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-bottom:6px}
  .track{position:relative;height:9px;border-radius:999px;
    background:linear-gradient(90deg,var(--neg-bg),var(--line) 50%,var(--pos-bg));border:1px solid var(--line)}
  .zero{position:absolute;top:-3px;bottom:-3px;left:50%;width:1.5px;background:var(--faint);opacity:.6}
  .target{position:absolute;top:-4px;bottom:-4px;width:2px;background:var(--pos);border-radius:2px}
  .marker{position:absolute;top:50%;width:14px;height:14px;border-radius:50%;background:var(--neg);
    border:2.5px solid var(--card);transform:translate(-50%,-50%);box-shadow:0 0 0 1px var(--neg)}
  .marker.up{background:var(--pos);box-shadow:0 0 0 1px var(--pos)}
  .plan{display:flex;gap:10px;margin-top:14px;padding:11px 12px;background:var(--accent-soft);
    border-radius:var(--r-sm);font-size:13px}
  .plan .ic{color:var(--accent);flex:none;font-weight:700}
  .plan b{font-weight:650}
  .resolved{padding:10px 16px;font-size:12.5px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 14px;border-top:1px solid var(--line)}
  .resolved b{color:var(--ink);font-weight:600}
  /* stat stack */
  .stat{padding:13px 16px;display:flex;align-items:baseline;justify-content:space-between;gap:10px}
  .stat + .stat{border-top:1px solid var(--line)}
  .stat .k{font-size:13px;color:var(--muted)}
  .stat .v{font-size:17px;font-weight:700;font-family:var(--mono);font-variant-numeric:tabular-nums}
  .badge{font-size:11px;font-weight:650;padding:2px 8px;border-radius:999px;background:var(--pos-bg);color:var(--pos)}
  .wkline{padding:10px 16px;border-top:1px solid var(--line);font-size:12.5px;color:var(--muted);font-variant-numeric:tabular-nums}
  .wkline .wklbl{color:var(--faint);font-weight:650;margin-right:6px}
  /* controls */
  .ctl{padding:14px 16px}
  .ctl-lab{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);font-weight:600;margin-bottom:8px}
  .wl{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:9px}
  .wl-tag{display:inline-flex;align-items:center;gap:6px;font-family:var(--mono);font-size:12.5px;font-weight:650;
    background:var(--raise);border:1px solid var(--line);padding:3px 6px 3px 9px;border-radius:var(--r-sm)}
  .wl-x{cursor:pointer;color:var(--faint);border:0;background:none;font:inherit;padding:0 2px;line-height:1}
  .wl-x:hover{color:var(--neg)}
  .row{display:flex;gap:7px;align-items:center}
  input.f{flex:1;min-width:0;background:var(--raise);border:1px solid var(--line);border-radius:8px;
    padding:7px 10px;color:var(--ink);font:inherit;font-size:13px}
  input.f:focus-visible{outline:2px solid var(--accent);outline-offset:1px;border-color:var(--accent)}
  input.wk{width:74px;flex:none;text-align:right;font-family:var(--mono)}
  .go{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit;font-weight:600;font-size:13px;flex:none}
  .go:hover{filter:brightness(1.06)} .go:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
  .hint{font-size:11.5px;color:var(--faint);margin-top:7px}
  .say{font-size:12px;margin-top:7px;min-height:16px}
  .say.ok{color:var(--pos)} .say.err{color:var(--neg)}
  .scanline{display:flex;align-items:center;gap:7px;font-size:12.5px;color:var(--muted);margin-top:6px}
  .tvrow{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 0;border-bottom:1px solid var(--line);font-size:12.5px}
  .tvrow:last-of-type{border-bottom:0}
  .tvrow b{font-family:var(--mono);font-weight:650}
  .tvlv{color:var(--muted);font-family:var(--mono);font-variant-numeric:tabular-nums;flex:1;text-align:center}
  .tvfresh{color:var(--pos);font-weight:600} .tvstale{color:var(--warn);font-weight:600}
  .tvmiss{color:var(--neg);font-size:12px}
  .tvcruft{font-size:11px;color:var(--faint);margin-top:9px}
  .scr-filters{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-top:10px}
  .scr-filters label{display:flex;flex-direction:column;gap:3px;font-size:10.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em}
  input.scrn{width:82px;font-family:var(--mono);text-align:right}
  /* activity */
  .feed{padding:4px 16px 12px}
  .ev{display:flex;gap:12px;padding:11px 0;border-bottom:1px solid var(--line)}
  .ev:last-child{border-bottom:0}
  .ev-ic{flex:none;width:28px;height:28px;border-radius:8px;display:grid;place-items:center;font-size:13px;
    background:var(--raise);border:1px solid var(--line)}
  .ev-ic.win{background:var(--pos-bg);border-color:transparent}
  .ev-ic.info{background:var(--accent-soft);border-color:transparent}
  .ev-b{flex:1;min-width:0}
  .ev-t{font-size:13.5px;font-weight:600}
  .ev-d{font-size:12.5px;color:var(--muted);margin-top:1px;overflow-wrap:anywhere}
  .ev-when{font-size:11.5px;color:var(--faint);white-space:nowrap;flex:none}
  /* detail tables */
  .more-body{padding:0 16px 16px;overflow-x:auto}
  .more-body h3{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin:18px 0 8px}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
  th{color:var(--faint);font-weight:600;font-size:10.5px;text-transform:uppercase;letter-spacing:.05em}
  tr:last-child td{border-bottom:0}
  td.num,th.num{text-align:right;font-family:var(--mono);font-variant-numeric:tabular-nums}
  .tag{font-size:11px;padding:1px 7px;border-radius:6px;border:1px solid var(--line)}
  .tag.win{color:var(--pos);border-color:transparent;background:var(--pos-bg)}
  .tag.loss{color:var(--neg);border-color:transparent;background:var(--neg-bg)}
  .tag.open{color:var(--accent)} .tag.assigned,.tag.closed{color:var(--warn)}
  .reason{white-space:normal}
  .foot{font-size:11.5px;color:var(--faint);text-align:center;padding:4px 0 2px}
  .foot code{font-family:var(--mono);background:var(--raise);padding:1px 5px;border-radius:4px}
  /* ---- readability: larger table text, touch-sized rows, sticky first column on wide tables ---- */
  table{font-size:13.5px}
  th,td{padding:9px 10px}
  th{font-size:11px}
  .more-body{position:relative}
  .more-body th:first-child,.more-body td:first-child{position:sticky;left:0;background:var(--card);z-index:1;
    box-shadow:1px 0 0 var(--line)}
  tbody tr:nth-child(even) td{background:color-mix(in srgb,var(--raise) 60%,var(--card))}
  .hint{font-size:12.5px;line-height:1.5;padding:0 16px 14px}
  .ctl .hint{padding:0;margin-top:7px}
  .stat .k{font-size:14px} .stat .v{font-size:19px}
  /* pills for categorical reads (bias, live) */
  .pill{display:inline-block;font-size:11.5px;font-weight:650;padding:2px 9px;border-radius:999px;
    border:1px solid var(--line);color:var(--muted);background:var(--raise);white-space:nowrap}
  .pill.fav{color:var(--pos);background:var(--pos-bg);border-color:transparent}
  .pill.avoid{color:var(--neg);background:var(--neg-bg);border-color:transparent}
  .pill.mixed{color:var(--warn);background:var(--warn-bg);border-color:transparent}
  .pill.live{color:var(--warn);background:var(--warn-bg);border-color:transparent}
  .pill.up{color:var(--pos);background:var(--pos-bg);border-color:transparent}
  .lbl{display:inline-block;font-size:12px;padding:1px 7px;margin:1px 3px 1px 0;border-radius:6px;
    background:var(--accent-soft);color:var(--accent);white-space:nowrap}
  .lbl.avoid{background:var(--neg-bg);color:var(--neg)}
  .lbl.fav{background:var(--pos-bg);color:var(--pos)}
  /* collapsible cards: tap the header to fold a section; state remembered per device */
  .card-h.clp{cursor:pointer;user-select:none;-webkit-tap-highlight-color:transparent}
  .card-h.clp h2{display:flex;align-items:center;gap:8px}
  .chev{display:inline-block;width:8px;height:8px;border-right:2px solid var(--faint);border-bottom:2px solid var(--faint);
    transform:rotate(45deg);transition:transform .15s;margin-top:-3px;flex:none}
  .card2.collapsed .chev{transform:rotate(-45deg);margin-top:2px}
  .card2.collapsed > :not(.card-h){display:none}
  .card2.collapsed .card-h{padding-bottom:14px}
  .sub-h{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:16px 16px 8px;
    margin-top:6px;border-top:1px solid var(--line);font-size:13px;font-weight:650}
  .sub-h .count{font-weight:600}
  /* brief archive */
  #brief .card-h{flex-wrap:wrap}
  .brief-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  select.f{background:var(--raise);border:1px solid var(--line);border-radius:8px;padding:7px 10px;color:var(--ink);
    font:inherit;font-size:13px;max-width:100%}
  .brief-meta{padding:10px 16px 0;font-size:12.5px;color:var(--muted);display:flex;gap:6px 14px;flex-wrap:wrap}
  .brief-meta b{color:var(--ink);font-weight:600}
  #brief-body{max-width:74ch;font-size:14.5px;line-height:1.6}
  #brief-body h2{font-size:17px;margin:4px 0 8px}
  #brief-body h3{font-size:14.5px;margin:18px 0 6px;padding-bottom:4px;border-bottom:1px solid var(--line)}
  #brief-body h4{font-size:13.5px;margin:12px 0 4px;color:var(--muted)}
  /* tab bar: top on desktop, thumb-reach bottom bar on phones */
  .tab-btn .ti{display:none}
  @media (max-width:760px){
    .tabs{position:fixed;top:auto;bottom:0;left:0;right:0;margin:0;z-index:40;
      padding:6px 8px calc(6px + env(safe-area-inset-bottom,0px));gap:2px;justify-content:space-around;
      border-top:1px solid var(--line);border-bottom:0;background:color-mix(in srgb,var(--card) 94%,transparent)}
    .tab-btn{flex:1 1 0;padding:6px 2px;font-size:10.5px;border:0;background:transparent;border-radius:10px;
      display:flex;flex-direction:column;align-items:center;gap:2px;min-width:0}
    .tab-btn .ti{display:block;font-size:19px;line-height:1.1}
    .tab-btn.active{background:var(--accent-soft);color:var(--accent);border-color:transparent}
    .nav-dot{display:none}
    .app{padding-bottom:calc(76px + env(safe-area-inset-bottom,0px))}
    .masthead h1{font-size:17px}
    .fact .v{font-size:13.5px}
    .scr-filters label{flex:1 1 40%}
  }
</style>
</head>
<body>
<div class="app"><div class="shell">

  <div class="masthead">
    <h1>Trader Cortex <span>· wheel bot</span></h1>
    <div class="updated"><span id="ago">loading…</span>
      <a class="rbtn" href="/calculator" style="text-decoration:none">Calculator</a>
      <button class="rbtn" onclick="loadAll()">Refresh now</button></div>
  </div>

  <div class="ribbon">
    <div class="ribbon-top" id="conn-top"><span class="conn"><span class="dot"></span> Checking connection…</span></div>
    <div class="ribbon-note" id="conn-note">This banner turns <b>red</b> the moment the bot loses its Robinhood connection or drops to the simulator.</div>
  </div>

  <nav class="tabs" id="tabs">
    <span class="nav-dot" id="nav-status" title="Robinhood connection"></span>
    <button class="tab-btn" data-tab="overview"><span class="ti">&#9673;</span>Overview</button>
    <button class="tab-btn" data-tab="research"><span class="ti">&#9776;</span>Watchlist</button>
    <button class="tab-btn" data-tab="brief"><span class="ti">&#9636;</span>Brief</button>
    <button class="tab-btn" data-tab="tuning"><span class="ti">&#9881;</span>Tuning</button>
    <button class="tab-btn" data-tab="history"><span class="ti">&#8635;</span>History</button>
  </nav>

  <div class="tabpane" id="pane-overview">
  <div class="grid">
    <div class="col">
      <section class="card2">
        <div class="card-h"><h2>What you're holding</h2><span class="count" id="pos-count"></span></div>
        <div id="holds"></div>
        <div id="resolved"></div>
      </section>
      <section class="card2">
        <div class="card-h"><h2>Recent activity</h2></div>
        <div class="feed" id="feed"></div>
      </section>
    </div>

    <div class="col">
      <section class="card2">
        <div class="card-h"><h2>Overall</h2><span class="count">all-time · real trades</span></div>
        <div id="week"></div>
      </section>
      <section class="card2" id="reserve-card">
        <div class="card-h"><h2>Tax reserve</h2><span class="count" id="rsv-note"></span></div>
        <div id="reserve"></div>
      </section>
      <section class="card2" id="tiers-card" data-fold="closed">
        <div class="card-h"><h2>Ready to add</h2><span class="count" id="tiers-note"></span></div>
        <div class="ctl" id="tiers"></div>
      </section>
    </div>
  </div>
  </div>

  <div class="tabpane" id="pane-research" hidden>
  <section class="card2" id="setups">
    <div class="card-h"><h2>Setups today</h2><span class="count" id="setups-note">deterministic daily-bar reads</span></div>
    <div class="more-body" style="padding:0">
      <table id="setups-tbl"><thead><tr>
        <th>Symbol</th><th class="num">Price</th><th>Setup(s)</th><th>Bias</th><th>Live</th>
        <th class="num">RSI</th><th class="num">%B</th><th class="num">BBw</th><th class="num">Vol&times;</th>
        <th class="num">Support</th><th class="num">Resist</th>
      </tr></thead><tbody><tr><td colspan="11" class="muted">Waiting for the next scan.</td></tr></tbody></table>
    </div>
    <div class="hint">Washout (oversold), coiling (volatility squeeze), breakout &amp; breakdown (confirmed on volume), and support tests, read from completed daily bars. <b>Live</b> = today's unfinished bar breaking the range or support right now. These feed the bot through the opt-in setup gates and the <i>prefer setups</i> tilt (Tuning tab); otherwise descriptive.</div>
    <div class="sub-h">Which setups have edge here <span class="count" id="setups-acc-note"></span></div>
    <div class="more-body" style="padding:0">
      <table id="setups-acc-tbl"><thead><tr>
        <th>Setup</th><th>Bias</th><th class="num">n</th><th class="num">Episodes</th><th class="num">Hit 5d</th>
        <th class="num">Avg 5d</th><th class="num">Avg 10d</th><th class="num">MAE 10d</th><th class="num">Pending</th>
      </tr></thead><tbody><tr><td colspan="9" class="muted">No resolved fires yet — outcomes fill in ~10 trading days after each setup fires.</td></tr></tbody></table>
    </div>
    <div class="hint">Measured on <b>your</b> names: each fire is logged, then scored on what actually happened 5 and 10 bars later. Hit = a favorable setup didn't fall / an avoid setup did. <b>MAE</b> = worst 10-day excursion below the fire price — the put-seller's question. Rows are greyed until n &ge; 15.</div>
    <div class="sub-h">Per-name risk profile <span class="count" id="rp-note"></span></div>
    <div class="more-body" style="padding:0">
      <table id="rp-tbl"><thead><tr>
        <th>Symbol</th><th class="num">n</th><th class="num">Touched</th><th class="num">ITM at exp</th>
        <th class="num">Avg worst</th><th class="num">Suggested cushion</th><th>Note</th>
      </tr></thead><tbody><tr><td colspan="7" class="muted">Profiles compute on each name's first scan of the day.</td></tr></tbody></table>
    </div>
    <div class="hint">Strike survival over ~1y: a put <span id="rp-base"></span> expected-moves below spot, held <span id="rp-h"></span> bars — how often it was <b>touched</b> (roll pressure) and <b>finished ITM</b> (assignment), plus the smallest cushion that keeps ITM under the <span id="rp-target"></span> target. Every watchlist name — including a new add — is profiled automatically. Apply suggestions from the Tuning tab.</div>
  </section>

  <section class="card2">
    <div class="card-h"><h2>TradingView levels</h2><span class="count" id="tv-last"></span></div>
    <div class="ctl" id="tv-levels"></div>
  </section>

  <section class="card2">
    <div class="card-h"><h2>Scanner</h2></div>
    <div class="ctl">
      <div class="ctl-lab">Watching for new puts <span id="wl-count" class="count"></span></div>
      <div class="wl" id="wl"></div>
      <div class="row">
        <input class="f" id="wl-in" placeholder="Add ticker, e.g. AAPL" maxlength="6"
          autocapitalize="characters" autocomplete="off"/>
        <button class="go" id="wl-add">Add</button>
      </div>
      <div class="hint">Aim for 10–25 names you'd be happy to own. Changes apply live.</div>
      <div class="say" id="wl-say"></div>

      <div class="ctl-lab" style="margin-top:16px">Weekly premium target</div>
      <div class="row">
        <input class="f wk mono" id="wk-in" inputmode="decimal" placeholder="0"/>
        <span class="muted" style="font-size:13px">% of account</span>
        <button class="go" id="wk-save">Save</button>
      </div>
      <div class="hint">Past this, new entries wait for your one-tap OK instead of auto-firing. 0 = off.</div>
      <div class="say" id="wk-say"></div>

      <div class="scanline" id="scanline"></div>
    </div>
  </section>




  <section class="card2" id="screener">
    <div class="card-h"><h2>Screener</h2><span class="count">on-demand CSP scan</span></div>
    <div class="ctl">
      <input class="f" id="scr-syms" placeholder="Symbols, comma-separated — leave blank to use your watchlist"/>
      <div class="scr-filters">
        <label>&Delta; min<input class="f scrn" id="scr-dmin" value="0.15" inputmode="decimal"/></label>
        <label>&Delta; max<input class="f scrn" id="scr-dmax" value="0.30" inputmode="decimal"/></label>
        <label>DTE min<input class="f scrn" id="scr-tmin" value="5" inputmode="numeric"/></label>
        <label>DTE max<input class="f scrn" id="scr-tmax" value="21" inputmode="numeric"/></label>
        <label>Min yield %<input class="f scrn" id="scr-yld" value="15" inputmode="decimal"/></label>
        <label>$ min<input class="f scrn" id="scr-pmin" value="7" inputmode="decimal"/></label>
        <label>$ max<input class="f scrn" id="scr-pmax" value="20" inputmode="decimal"/></label>
        <button class="go" id="scr-run">Screen symbols</button>
        <button class="go" id="scr-scan">Scan Alpaca $-range</button>
      </div>
      <div class="hint"><b>Screen symbols</b> = your list (or watchlist). <b>Scan Alpaca $-range</b> = discover the most-active names in the $min–$max band and screen them all. Ranked by theta-efficiency; read-only.</div>
      <div class="say" id="scr-say"></div>
      <div class="more-body" style="padding:0;margin-top:6px">
        <table id="scr-tbl"><thead><tr>
          <th>Symbol</th><th class="num">Price</th><th class="num">Strike</th><th class="num">DTE</th><th class="num">&Delta;</th>
          <th class="num">Premium</th><th class="num">Ann&nbsp;%</th><th class="num">&theta;</th>
          <th class="num">OI</th><th class="num">Vol</th><th class="num">Break-even</th>
        </tr></thead><tbody><tr><td colspan="11" class="muted">Screen your symbols, or scan Alpaca's $-range for opportunities.</td></tr></tbody></table>
      </div>
    </div>
  </section>

  <section class="card2" id="quality">
    <div class="card-h"><h2>Company Quality</h2><span class="count" id="q-note">informational — not a trade input</span></div>
    <div class="more-body" style="padding:0">
      <table id="quality-tbl"><thead><tr>
        <th>Symbol</th><th>Sector</th><th class="num">Score</th>
        <th class="num">Gross&nbsp;M</th><th class="num">Net&nbsp;M</th><th class="num">Rev&nbsp;gr</th>
        <th class="num">FCF&nbsp;M</th><th class="num">GP/A</th><th class="num">Insider&nbsp;90d</th>
      </tr></thead><tbody><tr><td colspan="9" class="muted">Enable entry.quality_scoring to compute a per-name quality read.</td></tr></tbody></table>
    </div>
    <div class="hint">A 0–100 read on each watchlist name's fundamentals — margins, cash flow, revenue growth, insider buying — so you can eyeball "is this actually a decent company to own if assigned." Purely informational: it does <b>not</b> gate or rank trades.</div>
  </section>
  </div>

  <div class="tabpane" id="pane-brief" hidden>
  <section class="card2" id="brief" data-nofold="1">
    <div class="card-h"><h2>Weekly Tactical Brief</h2>
      <div class="brief-tools">
        <select class="f" id="brief-list" title="Saved briefs"><option value="">Saved briefs…</option></select>
        <button class="go" id="brief-run">Generate new</button>
      </div>
    </div>
    <div class="brief-meta" id="brief-meta"></div>
    <div class="more-body" style="padding:12px 16px 16px">
      <div id="brief-body" class="muted">Monday prep: market backdrop, this week's catalysts, per-name levels + the strikes the bot's rules are eyeing, and advisory ideas across your accounts. Every brief you generate is <b>saved</b> — pick an earlier one from the list, or tap <b>Generate new</b> (reads your accounts live — takes a few seconds). Descriptive only — not financial advice.</div>
    </div>
  </section>
  </div>

  <div class="tabpane" id="pane-tuning" hidden>
  <section class="card2">
    <div class="card-h"><h2>Tuning</h2><span class="count">applies live · persists</span></div>
    <div class="ctl">
      <div class="ctl-lab">Tax reserve / gains sweep</div>
      <div class="row"><label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="tn-tr-on"/> Sweep a share of net realized gains into a symbol of your choice each week</label></div>
      <div class="scr-filters">
        <label>% of net gains<input class="f scrn" id="tn-tr-pct" inputmode="decimal" placeholder="20"/></label>
        <label>Symbol<input class="f scrn" id="tn-tr-sym" placeholder="SGOV" maxlength="6" autocapitalize="characters"/></label>
        <label>Day<select class="f" id="tn-tr-day"><option value="0">Mon</option><option value="1">Tue</option><option value="2">Wed</option><option value="3">Thu</option><option value="4">Fri</option></select></label>
        <label>Time (ET)<input class="f scrn" id="tn-tr-time" placeholder="15:40"/></label>
        <button class="go" id="tn-tr-save">Save</button>
      </div>
      <div class="row" style="margin-top:8px"><label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="tn-tr-dry"/> Dry run (log what it would buy, place nothing)</label></div>
      <div class="hint">Each week at the set time (inside market hours, since share market orders only fill then) the bot adds up the realized P&amp;L closed since the last sweep, carries any loss forward, and buys that share of a positive net in the ETF. The reserve is walled off: it never counts as trading capital, never gets calls written on it, and the bot never sells it. Withdraw it yourself when taxes are due.</div>
      <div class="say" id="tn-tr-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Market regime</div>
      <div class="row"><label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="tn-skip-dt"/> Pause new puts in a confirmed downtrend</label></div>
      <div class="row" style="margin-top:8px">
        <span class="muted" style="font-size:13px">SPY below its 200-day for</span>
        <input class="f wk mono" id="tn-dt-days" inputmode="numeric" placeholder="5"/>
        <span class="muted" style="font-size:13px">straight sessions</span>
        <button class="go" id="tn-dt-save">Save</button>
      </div>
      <div class="scanline" id="tn-dt-status"></div>
      <div class="hint">Measured on your names since 2020: once SPY has closed below its 200-day for 5 sessions, a put 0.7 expected-moves out earned about +0.1% per trade against +0.9% elsewhere, with the most assignments. Skipping only those days (panic regimes stay open, they paid best) lifted P&amp;L per trade from +0.89% to +1.00% and cut total losses 19%. New put entries only; open positions, rolls and covered calls carry on.</div>
      <div class="say" id="tn-dt-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Multiple CSPs per ticker</div>
      <div class="row">
        <input class="f wk mono" id="tn-uc" inputmode="decimal" placeholder="0"/>
        <span class="muted" style="font-size:13px">% max per name</span>
        <button class="go" id="tn-uc-save">Save</button>
      </div>
      <div class="hint">Max % of account in one ticker's short-put collateral — laddered strikes / more contracts, built in a single scan (no adding over days). 0 = one CSP per name (default).</div>
      <div class="say" id="tn-uc-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Entry quality gates</div>
      <div class="scr-filters">
        <label>Min cushion (exp-moves)<input class="f scrn" id="tn-em" inputmode="decimal" placeholder="off"/></label>
        <label>Min IV / realized vol<input class="f scrn" id="tn-vrp" inputmode="decimal" placeholder="off"/></label>
        <button class="go" id="tn-gates-save">Save gates</button>
      </div>
      <div class="hint"><b>Cushion</b>: require the strike to sit at least N option-implied expected-moves out of the money (scales to each name's volatility). <b>IV/RV</b>: only sell when implied vol beats realized by this ratio (e.g. 1.1). Blank = off.</div>
      <div class="say" id="tn-gates-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Setup gates</div>
      <div class="scr-filters">
        <label>Avoid setups<input class="f" id="tn-avoid" placeholder="e.g. breakdown_confirmed, support_break"/></label>
        <label>Require setups<input class="f" id="tn-require" placeholder="e.g. washout_at_support"/></label>
        <button class="go" id="tn-setups-save">Save</button>
      </div>
      <div class="row" style="margin-top:8px"><label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="tn-prefer-setups"/> Prefer favorable setups when ranking (reorders only)</label></div>
      <div class="hint">Comma-separated labels from the Setups panel. <b>Avoid</b> skips a name while any listed label is active (e.g. a fresh breakdown); <b>Require</b> enters only when one is active. Blank = off (fail-open when a name has no read).</div>
      <div class="row" style="margin-top:8px">
        <button class="go" id="tn-preset-putseller">Apply put-seller preset</button>
        <span class="muted" style="font-size:12.5px">avoid the breakout family — measured highest assignment rate on your names</span>
      </div>
      <div class="say" id="tn-setups-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Per-ticker cushions <span class="count" id="tn-cush-note"></span></div>
      <div id="tn-cush-list" class="tvcruft"></div>
      <div class="row" style="margin-top:8px">
        <button class="go" id="tn-cush-apply">Apply suggested cushions</button>
        <span class="muted" style="font-size:12.5px">sets each name's min expected-move cushion (tightening only)</span>
      </div>
      <div class="hint">From the per-name risk profile: names whose historical assignment rate at the base cushion runs above target get a wider <i>min_strike_expected_moves</i> override. Never loosens an existing override.</div>
      <div class="say" id="tn-cush-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Global screening</div>
      <div class="scr-filters">
        <label>&Delta; min<input class="f scrn" id="tn-dmin" inputmode="decimal"/></label>
        <label>&Delta; max<input class="f scrn" id="tn-dmax" inputmode="decimal"/></label>
        <button class="go" id="tn-global-save">Save</button>
      </div>
      <div class="row" style="margin-top:8px"><label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer"><input type="checkbox" id="tn-ivrank"/> Prefer rich IV rank (rank by IV vs each name's own history)</label></div>
      <div class="hint">The short-put delta band the scanner targets. Changes apply to the next scan.</div>
      <div class="say" id="tn-global-say"></div>

      <div class="ctl-lab" style="margin-top:18px">Per-ticker overrides <span id="tn-pt-cur" class="count"></span></div>
      <div class="row">
        <input class="f" id="tn-pt-sym" placeholder="Symbol, e.g. BULL" maxlength="6" autocapitalize="characters" autocomplete="off"/>
      </div>
      <div class="scr-filters">
        <label>&Delta; max<input class="f scrn" id="tn-pt-dmax" inputmode="decimal" placeholder="—"/></label>
        <label>Min IV rank<input class="f scrn" id="tn-pt-ivr" inputmode="decimal" placeholder="—"/></label>
        <label>Min IV / RV<input class="f scrn" id="tn-pt-vrp" inputmode="decimal" placeholder="—"/></label>
        <label>Min cushion<input class="f scrn" id="tn-pt-em" inputmode="decimal" placeholder="—"/></label>
        <button class="go" id="tn-pt-save">Set overrides</button>
      </div>
      <div class="hint">Tailor one name (merged over the global criteria). Fill only what you want to override; blank = leave as-is. Removing an override still needs a config edit.</div>
      <div id="tn-pt-list" class="tvcruft"></div>
      <div class="say" id="tn-pt-say"></div>
    </div>
  </section>
  </div>

  <div class="tabpane" id="pane-history" hidden>
  <section class="card2">
    <div class="card-h"><h2>Per-rule performance</h2></div>
    <div class="more-body">
      <table id="rules"><thead><tr><th>Rule</th><th class="num">Closes</th><th class="num">Wins</th>
        <th class="num">Win&nbsp;%</th><th class="num">Realized&nbsp;P&amp;L</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2">
    <div class="card-h"><h2>All positions</h2></div>
    <div class="more-body">
      <table id="positions"><thead><tr><th>Symbol</th><th>Strategy</th><th>Status</th><th class="num">Qty</th>
        <th class="num">Credit</th><th class="num">Close</th><th class="num">P&amp;L</th><th class="num">DTE</th>
        <th>Outcome</th><th>Rule</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2">
    <div class="card-h"><h2>Decision log — the "why"</h2></div>
    <div class="more-body">
      <table id="decisions"><thead><tr><th>When</th><th>Rule</th><th>Reason</th><th>Approval</th><th>Status</th>
        </tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2" data-fold="closed">
    <div class="card-h"><h2>Opportunities — latest CSP scan</h2></div>
    <div class="more-body">
      <table id="candidates"><thead><tr><th>Symbol</th><th class="num">Strike</th><th class="num">DTE</th>
        <th class="num">&Delta;</th><th class="num">Premium</th><th class="num">Ann&nbsp;%</th>
        <th class="num">OI</th><th class="num">IVR</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2" data-fold="closed">
    <div class="card-h"><h2>Holdings — shares &amp; covered-call coverage</h2></div>
    <div class="more-body">
      <table id="holdings"><thead><tr><th>Symbol</th><th class="num">Shares</th><th class="num">Cost basis</th>
        <th class="num">Coverable</th><th class="num">Covered</th><th>Note</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2" data-fold="closed">
    <div class="card-h"><h2>CC opportunities — calls on shares you hold</h2></div>
    <div class="more-body">
      <table id="cc-candidates"><thead><tr><th>Symbol</th><th class="num">Strike</th><th class="num">DTE</th>
        <th class="num">&Delta;</th><th class="num">Premium</th><th class="num">Ann&nbsp;%</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2" data-fold="closed">
    <div class="card-h"><h2>Entry log — auto-entered CSPs &amp; CCs</h2></div>
    <div class="more-body">
      <table id="entries"><thead><tr><th>When</th><th>Symbol</th><th class="num">Qty</th><th class="num">Strike</th>
        <th class="num">Premium</th><th>Status</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  <section class="card2" data-fold="closed">
    <div class="card-h"><h2>Trade journal — labeled entries + outcomes (learning data)</h2></div>
    <div class="more-body">
      <table id="journal"><thead><tr><th>Entered</th><th>Kind</th><th>Symbol</th><th class="num">&Delta;</th>
        <th class="num">DTE</th><th class="num">IV</th><th class="num">Premium</th><th>Status</th>
        <th class="num">P&amp;L</th></tr></thead><tbody></tbody></table>
    </div>
  </section>
  </div>

  <div class="foot">Read-only monitoring · settings changes are logged · powered by AgenticRobinhood</div>
</div></div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const money = (v) => v == null ? "—"
  : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = (v) => v == null ? "muted" : v > 0 ? "pos-c" : v < 0 ? "neg" : "";
const pct = (v) => v == null ? "—" : (v*100).toFixed(0) + "%";
const human = (l) => String(l == null ? "" : l).split("_").join(" ");
const biasPill = (b) => { const k = b==="favorable"?"fav":(b==="avoid"?"avoid":(b==="mixed"?"mixed":"")); return `<span class="pill ${k}">${esc(b||"none")}</span>`; };
async function getJSON(u){ const r = await fetch(u,{credentials:"same-origin"}); if(!r.ok) throw new Error(u+" -> "+r.status); return r.json(); }
function tickerLogo(sym){
  const s = esc(sym);
  const hue = [...s].reduce((a,c)=>a+c.charCodeAt(0),0)%360;
  return `<span class="tlogo">`
    + `<img src="https://assets.parqet.com/logos/symbol/${s}?format=png&size=52" alt="" loading="lazy" `
    + `onerror="this.style.display='none';this.nextElementSibling.style.display='grid'">`
    + `<span class="tmono" style="display:none;background:hsl(${hue},45%,45%)">${(s[0]||'?').toUpperCase()}</span></span>`;
}

/* ---- connection hero ---- */
async function loadConn(){
  const top = $("conn-top"), note = $("conn-note");
  let st = {}, bs = {};
  try { st = await getJSON("/control/status"); } catch(e){
    top.innerHTML = `<span class="conn bad"><span class="dot"></span> Can't reach the bot</span>`;
    note.innerHTML = `The dashboard couldn't load the bot's status. It may be redeploying — try again in a minute.`;
    const nd=$("nav-status"); if(nd) nd.className="nav-dot bad"; return;
  }
  try { bs = await getJSON("/control/broker-status"); } catch(e){ bs = {ok:false}; }
  let sc = null; try { sc = await getJSON("/api/scan-status"); } catch(e){ sc = null; }
  const paper = bs.is_paper === true, live = st.mode === "live";
  let scanChip = "";
  if(sc){
    const lastAgo = sc.last_scan_at ? agoStr((Date.now()-new Date(sc.last_scan_at).getTime())/1000) : null;
    scanChip = sc.market_open
      ? `<span class="chip good">Market open · scanning${lastAgo?" <b>"+esc(lastAgo)+"</b>":""}</span>`
      : `<span class="chip">Market closed${lastAgo?" · last scan <b>"+esc(lastAgo)+"</b>":""}</span>`;
    if(sc.last_error) scanChip += `<span class="chip warn" title="${esc(sc.last_error)}">scan error</span>`;
  }
  let klass="", label="", sub="";
  if(bs.read_error){ klass="bad"; label="Broker connection problem"; sub="· "+bs.read_error; }
  else if(paper && live){ klass="bad"; label="Running on the SIMULATOR"; sub="· not trading your real account"; }
  else if(paper){ klass="warn"; label="Paper (simulation) mode"; sub="· no real orders"; }
  else if(bs.broker==="robinhood_mcp"){ klass=""; label="Connected to Robinhood"; sub="· real account"; }
  else { klass="warn"; label="Broker: "+esc(bs.broker||"unknown"); sub=""; }
  top.innerHTML = `<span class="conn ${klass}"><span class="dot"></span> ${esc(label)} <small>${esc(sub)}</small></span>
    <div class="chips">
      <span class="chip ${live?"good":""}">${live?"Live trading armed":"Paper mode"}</span>
      <span class="chip ${st.paused?"warn":""}">${st.paused?"⏸ Paused":"Running"}</span>
      ${scanChip}
      ${bs.buying_power!=null?`<span class="chip">Buying power <b>${money(bs.buying_power)}</b></span>`:""}
      ${bs.open_positions!=null?`<span class="chip">${bs.open_positions} open</span>`:""}
    </div>`;
  note.innerHTML = (klass==="bad")
    ? `<b style="color:var(--neg)">Heads up:</b> the bot is not managing your real Robinhood positions right now.`
    : `This banner turns <b>red</b> the moment the bot loses its Robinhood connection or drops to the simulator.`;
  const nd=$("nav-status"); if(nd) nd.className="nav-dot "+(klass||"good");
}

/* ---- holdings (plain english) ---- */
function planFor(p){
  const put = p.option_type === "PUT";
  if(p.dte <= 2) return "Near expiry — the bot will let it expire for the full credit or close it, and ping you.";
  if(put) return "Holding. The bot <b>auto-closes at +50% profit</b>, <b>rolls it for a credit</b> if the stock tests your strike near expiry, and <b>pings you at 2 days left</b>.";
  return "Covered call working. The bot manages it to expiry or buys it back at the profit target; if the shares get called away, it returns to selling puts.";
}
async function loadHolds(){
  const {positions} = await getJSON("/api/positions");
  const open = positions.filter(p => p.status==="OPEN" || p.status==="CLOSING");
  const done = positions.filter(p => !(p.status==="OPEN" || p.status==="CLOSING"));
  $("pos-count").textContent = open.length + (open.length===1?" open position":" open positions");
  $("holds").innerHTML = open.map(p => {
    const put = p.option_type === "PUT";
    const credit = p.credit_received*100*p.quantity;
    const nowVal = p.current_mark!=null ? p.current_mark*100*p.quantity : null;
    const upnl = p.unrealized_pnl;
    let gauge = "";
    if(nowVal!=null && credit>0){
      const capt = upnl/credit;                 // -1..+1 of credit
      const posp = Math.max(2,Math.min(98,(capt+1)/2*100));
      gauge = `<div class="gauge"><div class="gauge-lab"><span>Losing</span><span>Profit captured</span><span>Winning</span></div>
        <div class="track"><div class="zero"></div><div class="target" style="left:75%"></div>
        <div class="marker ${upnl>=0?"up":""}" style="left:${posp}%"></div></div></div>`;
    }
    return `<article class="pos-item">
      <div class="pos-top">
        <div><div class="tick">${tickerLogo(p.underlying)}${esc(p.underlying)} <span class="kind">$${p.strike} ${put?"put":"call"} · ${esc(p.strategy.replace(/_/g," ").toLowerCase())}</span></div>
          <div class="pos-sub">${p.quantity} contract${p.quantity>1?"s":""} · expires <b>${esc(new Date(p.expiration+"T00:00:00").toLocaleDateString(undefined,{month:"short",day:"numeric"}))}</b> · <b>${p.dte} day${p.dte===1?"":"s"} left</b></div></div>
        <div class="pnl"><div class="v ${cls(upnl)}">${money(upnl)}</div><div class="l">unrealized</div></div>
      </div>
      <div class="facts">
        <div class="fact"><div class="k">Credit collected</div><div class="v">${money(credit)}</div></div>
        <div class="fact"><div class="k">Cost to close now</div><div class="v">${nowVal!=null?money(nowVal):"—"}</div></div>
        <div class="fact"><div class="k">Days left</div><div class="v">${p.dte}</div></div>
      </div>${gauge}
      <div class="plan"><span class="ic">→</span><div><b>Plan:</b> ${planFor(p)}</div></div>
    </article>`;
  }).join("") || `<div class="pos-item muted">No open positions right now. The scanner will open new cash-secured puts when a watchlist name meets your rules.</div>`;

  const recent = done.slice(0,4).map(p => {
    const pnl = p.realized_pnl;
    const w = p.outcome==="win"||p.status==="EXPIRED";
    return `<span>${esc(p.underlying)} $${p.strike}${p.option_type[0]} <b class="${w?"pos-c":(p.outcome==="loss"?"neg":"")}">${esc(p.status.toLowerCase())}${pnl!=null?" "+money(pnl):""}</b></span>`;
  }).join("");
  $("resolved").innerHTML = recent ? `<div class="resolved"><span class="muted">Recently closed:</span> ${recent}</div>` : "";
}

/* ---- this week ---- */
async function loadWeek(){
  const s = await getJSON("/api/stats");
  const w = s.this_week || {};
  const wn = w.resolved_count || 0;
  const wk = wn
    ? `<span class="${cls(w.realized_pnl)}">${money(w.realized_pnl)}</span> realized · ${wn} closed · ${pct(w.win_rate)} win`
    : `<span class="v">no trades closed yet</span>`;
  $("week").innerHTML = `
    <div class="stat"><span class="k">Realized P&L</span><span class="v ${cls(s.realized_pnl)}">${money(s.realized_pnl)}</span></div>
    <div class="stat"><span class="k">Win rate</span><span class="v">${pct(s.win_rate)} <span class="badge">${s.wins} / ${s.resolved_count}</span></span></div>
    <div class="stat"><span class="k">Open now</span><span class="v">${s.open_count}</span></div>
    <div class="stat"><span class="k">Unrealized</span><span class="v ${cls(s.unrealized_pnl)}">${money(s.unrealized_pnl)}</span></div>
    <div class="wkline"><span class="wklbl">This week</span> ${wk}</div>`;
}

/* ---- tax reserve + capital unlocks ---- */
function dayName(i){ return ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][i] || ""; }
async function loadReserve(){
  const el = $("reserve"); if(!el) return;
  const d = await getJSON("/api/tax-reserve");
  const cfg = d.config || {}, h = d.holding, t = d.totals || {}, st = d.status || {};
  if($("rsv-note")) $("rsv-note").textContent = cfg.enabled ? (cfg.dry_run ? "on · dry run" : "on") : "off";
  const when = st.next_sweep_at ? new Date(st.next_sweep_at).toLocaleString(undefined,{weekday:"short",month:"short",day:"numeric",hour:"numeric",minute:"2-digit"}) : "—";
  const last = (d.recent||[])[0];
  el.innerHTML = `
    <div class="stat"><span class="k">Held in ${esc(cfg.symbol||"SGOV")}</span><span class="v">${h ? money(h.value) : "$0.00"}</span></div>
    <div class="stat"><span class="k">Swept to date</span><span class="v">${money(t.swept_dollars||0)} <span class="badge">${t.sweeps||0} sweeps</span></span></div>
    <div class="stat"><span class="k">Net realized since last sweep</span><span class="v ${cls(st.pending_net_since_last)}">${money(st.pending_net_since_last)}</span></div>
    <div class="stat"><span class="k">Next sweep would buy</span><span class="v">${money(st.would_sweep||0)}</span></div>
    <div class="wkline"><span class="wklbl">Next</span> ${esc(when)}${cfg.enabled ? "" : " · turn on in Tuning"}${last ? ` · last: ${esc(last.status)} ${last.dollar_amount ? money(last.dollar_amount) : ""}` : ""}</div>`;
}
async function loadTiers(){
  const el = $("tiers"); if(!el) return;
  const d = await getJSON("/api/tiers");
  const ready = d.ready || [];
  if($("tiers-note")) $("tiers-note").textContent = ready.length ? ready.length + " fit the per-name cap" : "none yet";
  const nxt = d.next ? `<div class="hint">Next unlock: <b>${esc(d.next.symbol)}</b> needs about ${money(d.next.account_value_needed)} of account value (one contract = ${money(d.next.collateral)}).</div>` : "";
  el.innerHTML = (ready.length ? ready.map(r => `<div class="row" style="margin-bottom:6px"><span class="wl-tag">${esc(r.symbol)}</span>
      <span class="muted" style="font-size:12.5px;flex:1">${money(r.collateral)} per contract · cap ${money(r.per_name_cap)}${r.note ? " · " + esc(r.note) : ""}</span>
      <button class="go" data-add="${esc(r.symbol)}" data-pt='${esc(JSON.stringify(r.per_ticker||{}))}'>Add</button></div>`).join("")
    : `<div class="muted" style="font-size:12.5px">Quality names from the community list appear here once one contract fits under the per-name cap. Nothing is added without a tap.</div>`) + nxt
    + `<div class="say" id="tiers-say"></div>`;
  el.querySelectorAll("button[data-add]").forEach(b => b.onclick = async () => {
    const sym = b.dataset.add; let pt = {}; try { pt = JSON.parse(b.dataset.pt||"{}"); } catch(e){}
    try { const cfg = await getJSON("/api/config"); const wl = (cfg.editable.entry.watchlist||[]).concat([sym]);
      await postConfig({entry:{watchlist: wl, per_ticker: {[sym]: pt}}});
      say("tiers-say", "Added "+sym+" with its tier overrides.", true); await loadControls(); await loadTiers();
    } catch(e){ say("tiers-say", "Couldn't add: "+e.message, false); }
  });
}
function fillReserve(tr){
  if(document.activeElement !== $("tn-tr-on")) $("tn-tr-on").checked = !!tr.enabled;
  if(document.activeElement !== $("tn-tr-dry")) $("tn-tr-dry").checked = tr.dry_run !== false;
  _setIf("tn-tr-pct", tr.pct != null ? (tr.pct*100).toFixed(0) : "20");
  _setIf("tn-tr-sym", tr.symbol || "SGOV");
  _setIf("tn-tr-day", tr.weekday != null ? String(tr.weekday) : "4");
  _setIf("tn-tr-time", (tr.hour != null ? String(tr.hour).padStart(2,"0") : "15") + ":" + (tr.minute != null ? String(tr.minute).padStart(2,"0") : "40"));
}
async function saveReserve(){
  const pct = parseFloat(($("tn-tr-pct").value||"").trim()); const sym = ($("tn-tr-sym").value||"").trim().toUpperCase();
  const tm = ($("tn-tr-time").value||"15:40").trim().split(":"); const hh = parseInt(tm[0],10), mm = parseInt(tm[1]||"0",10);
  if(isNaN(pct) || pct <= 0 || pct > 60){ say("tn-tr-say","Enter a percent between 1 and 60.",false); return; }
  if(!/^[A-Z]{1,6}$/.test(sym)){ say("tn-tr-say","Enter a stock or ETF ticker, e.g. SGOV.",false); return; }
  if(isNaN(hh) || isNaN(mm) || hh < 9 || hh > 15 || (hh === 9 && mm < 35)){ say("tn-tr-say","Time must be inside market hours (09:35 to 15:55 ET).",false); return; }
  try { await postConfig({tax_reserve:{enabled:$("tn-tr-on").checked, dry_run:$("tn-tr-dry").checked, pct: pct/100, symbol: sym, weekday: parseInt($("tn-tr-day").value,10), hour: hh, minute: mm}});
    say("tn-tr-say", $("tn-tr-on").checked ? (($("tn-tr-dry").checked ? "On (dry run): " : "On: ") + pct + "% of net gains into " + sym + " every " + dayName(parseInt($("tn-tr-day").value,10)) + ".") : "Off.", true);
    await loadReserve();
  } catch(e){ say("tn-tr-say","Couldn't save: "+e.message,false); }
}

/* ---- activity ---- */
const RULE_LABEL = {"profit-trail":"Took profit","profit-target":"Took profit","profit-50":"Took profit",
  "roll":"Rolled a position","dte-2":"Expiry check","dte-close":"Expiry close","stop-loss":"Stop-loss",
  "deep-itm-alert":"Deep-ITM alert","tv-signal":"TradingView signal","csp-screener":"Opened a put"};
const RULE_ICON = {"Took profit":["win","✓"],"Rolled a position":["", "↻"],"TradingView signal":["info","⚡"],
  "Opened a put":["info","+"]};
async function loadFeed(){
  const {decisions} = await getJSON("/api/decisions?limit=8");
  $("feed").innerHTML = decisions.map(d => {
    const label = RULE_LABEL[d.rule_name] || esc(d.rule_name);
    const ic = RULE_ICON[label] || ["","•"];
    const when = new Date(d.created_at);
    const ago = when.toLocaleDateString(undefined,{month:"short",day:"numeric"});
    return `<div class="ev"><div class="ev-ic ${ic[0]}">${ic[1]}</div>
      <div class="ev-b"><div class="ev-t">${label} <span class="muted" style="font-weight:500">· ${esc(d.status.toLowerCase())}</span></div>
      <div class="ev-d">${esc(d.reason)}</div></div><div class="ev-when">${esc(ago)}</div></div>`;
  }).join("") || `<div class="ev muted">Nothing yet — the bot logs every action here.</div>`;
}

/* ---- scanner status + controls ---- */
let watchlist = [];
async function loadScanStatus(){
  try {
    const s = await getJSON("/api/scan-status");
    const bits = [];
    bits.push(s.market_open ? "🟢 Market open — scanning" : "🌙 Market closed — resumes at the open");
    if(s.last_error) bits.push("⚠ " + esc(s.last_error));
    $("scanline").innerHTML = bits.map(b => `<span>${b}</span>`).join("");
  } catch(e){ $("scanline").textContent = ""; }
}
function agoStr(sec){
  if(sec==null) return "—";
  if(sec < 3600) return Math.round(sec/60)+"m ago";
  const h = sec/3600;
  return h < 48 ? h.toFixed(0)+"h ago" : (h/24).toFixed(1)+"d ago";
}
async function loadTvLevels(){
  const d = await getJSON("/api/tv-health");
  const lt = d.latest;
  $("tv-last").textContent = lt ? ("last: "+lt.symbol+" "+agoStr(lt.age_seconds)) : "none yet";
  const rows = (d.symbols||[]).map(s => {
    if(!s.present)
      return `<div class="tvrow"><b>${esc(s.symbol)}</b><span class="tvlv"></span><span class="tvmiss">no alert yet</span></div>`;
    return `<div class="tvrow"><b>${esc(s.symbol)}</b>
      <span class="tvlv">↓ ${s.support!=null?esc(s.support):"—"} · ↑ ${s.resistance!=null?esc(s.resistance):"—"}</span>
      <span class="${s.stale?"tvstale":"tvfresh"}">${agoStr(s.age_seconds)}</span></div>`;
  }).join("") || `<div class="muted" style="font-size:12.5px">No watchlist symbols.</div>`;
  const cruft = (d.cruft && d.cruft.length)
    ? `<div class="tvcruft">stored but not watched: ${d.cruft.map(esc).join(", ")}</div>` : "";
  $("tv-levels").innerHTML = rows + cruft;
}
function scrRender(cands){
  $("scr-tbl").querySelector("tbody").innerHTML = cands.map(c=>`<tr>
    <td>${esc(c.underlying)}</td>
    <td class="num">${c.price!=null?money(c.price):"—"}</td>
    <td class="num">${c.strike}</td><td class="num">${c.dte}</td>
    <td class="num">${c.delta!=null?Math.abs(c.delta).toFixed(2):"—"}</td>
    <td class="num">${money(c.premium)}</td>
    <td class="num">${c.annualized_ror!=null?c.annualized_ror.toFixed(0)+"%":"—"}</td>
    <td class="num">${c.theta!=null?Math.abs(c.theta).toFixed(3):"—"}</td>
    <td class="num">${c.open_interest!=null?c.open_interest:"—"}</td>
    <td class="num">${c.volume!=null?c.volume:"—"}</td>
    <td class="num">${money(c.break_even)}</td></tr>`).join("")
    || `<tr><td colspan="11" class="muted">No matches — loosen the filters.</td></tr>`;
}
async function runScreen(){
  const syms = ($("scr-syms").value||"").split(",").map(s=>s.trim().toUpperCase()).filter(Boolean);
  const num = (id)=>{ const v=parseFloat($(id).value); return isNaN(v)?null:v; };
  const body = {};
  if(syms.length) body.symbols = syms;
  const dmin=num("scr-dmin"), dmax=num("scr-dmax"), tmin=num("scr-tmin"), tmax=num("scr-tmax"), yld=num("scr-yld");
  if(dmin!=null) body.delta_min=dmin;
  if(dmax!=null) body.delta_max=dmax;
  if(tmin!=null) body.dte_min=tmin;
  if(tmax!=null) body.dte_max=tmax;
  if(yld!=null) body.min_annualized_yield=yld/100;
  const say=$("scr-say"); say.textContent="Screening… (fetching live chains)"; say.className="say";
  $("scr-run").disabled=true;
  try{
    const r=await fetch("/api/screen",{method:"POST",headers:{"Content-Type":"application/json"},
      credentials:"same-origin",body:JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok||!j.ok) throw new Error(j.error||("HTTP "+r.status));
    say.textContent=j.count+" candidate"+(j.count===1?"":"s")+" across "+j.symbols.length+" name"+(j.symbols.length===1?"":"s")+" — ranked by theta-efficiency";
    say.className="say ok";
    scrRender(j.candidates);
  }catch(e){ say.textContent="Screen failed: "+e.message; say.className="say err"; }
  finally{ $("scr-run").disabled=false; }
}
async function runScan(){
  const num=(id)=>{ const v=parseFloat($(id).value); return isNaN(v)?null:v; };
  const body={};
  const pmin=num("scr-pmin"), pmax=num("scr-pmax");
  if(pmin!=null) body.price_min=pmin;
  if(pmax!=null) body.price_max=pmax;
  const dmin=num("scr-dmin"), dmax=num("scr-dmax"), tmin=num("scr-tmin"), tmax=num("scr-tmax"), yld=num("scr-yld");
  if(dmin!=null) body.delta_min=dmin;
  if(dmax!=null) body.delta_max=dmax;
  if(tmin!=null) body.dte_min=tmin;
  if(tmax!=null) body.dte_max=tmax;
  if(yld!=null) body.min_annualized_yield=yld/100;
  const say=$("scr-say"); say.textContent="Scanning Alpaca $"+(pmin||"?")+"–$"+(pmax||"?")+" (universe + live chains, ~10–30s)…"; say.className="say";
  $("scr-scan").disabled=true;
  try{
    const r=await fetch("/api/opportunities",{method:"POST",headers:{"Content-Type":"application/json"},
      credentials:"same-origin",body:JSON.stringify(body)});
    const j=await r.json();
    if(!r.ok||!j.ok) throw new Error(j.error||("HTTP "+r.status));
    say.textContent=j.count+" opportunit"+(j.count===1?"y":"ies")+" from "+j.scanned.length+" in-band name"+(j.scanned.length===1?"":"s")+" (of "+j.universe+" most-active) — best theta first";
    say.className="say ok";
    scrRender(j.candidates);
  }catch(e){ say.textContent="Scan failed: "+e.message; say.className="say err"; }
  finally{ $("scr-scan").disabled=false; }
}
async function loadControls(){
  const cfg = await getJSON("/api/config");
  const e = cfg.editable.entry || {};
  watchlist = (e.watchlist || []).slice();
  renderWatchlist();
  const wt = e.weekly_premium_target_pct;
  if(document.activeElement !== $("wk-in")) $("wk-in").value = wt ? (wt*100).toFixed(wt*100 % 1 ? 1 : 0) : "0";
  fillTuning(e);
  fillMacro(cfg.editable.macro || {});
  fillReserve(cfg.editable.tax_reserve || {});
}
function fillMacro(m){
  if(document.activeElement !== $("tn-skip-dt")) $("tn-skip-dt").checked = !!m.skip_confirmed_downtrend;
  _setIf("tn-dt-days", m.downtrend_confirm_days != null ? m.downtrend_confirm_days : 5);
}
async function saveDowntrend(){
  const raw=($("tn-dt-days").value||"").trim(); const d=parseInt(raw||"5",10);
  if(isNaN(d) || d<1 || d>60){ say("tn-dt-say","Enter 1-60 sessions.",false); return; }
  try { await postConfig({macro:{skip_confirmed_downtrend:$("tn-skip-dt").checked, downtrend_confirm_days:d}});
    say("tn-dt-say", $("tn-skip-dt").checked ? ("On: new puts pause after "+d+" sessions below the 200-day.") : "Off: regime is informational only.", true);
    await loadRegimeStatus();
  } catch(e){ say("tn-dt-say","Couldn't save: "+e.message,false); }
}
async function loadRegimeStatus(){
  const el = $("tn-dt-status"); if(!el) return;
  try {
    const d = await getJSON("/api/regime"); const r = d.regime || {};
    if(r.spy_days_below_sma200 == null){ el.textContent = "SPY vs 200-day: not computed yet (first scan)."; return; }
    const on = d.skip_confirmed_downtrend;
    const state = r.spy_days_below_sma200 > 0 ? ("SPY has closed below its 200-day for "+r.spy_days_below_sma200+" session"+(r.spy_days_below_sma200===1?"":"s")) : "SPY is above its 200-day";
    el.innerHTML = `<span>${esc(state)}</span> <span class="pill ${r.confirmed_downtrend ? (on ? "avoid" : "mixed") : "fav"}">${r.confirmed_downtrend ? (on ? "new puts paused" : "confirmed downtrend (gate off)") : "clear"}</span> <span class="muted">regime: ${esc(r.label||"?")}${r.vix!=null?" · VIX "+Number(r.vix).toFixed(1):""}</span>`;
  } catch(e){ el.textContent = ""; }
}
function _setIf(id, v){ if(document.activeElement !== $(id)) $(id).value = (v==null ? "" : v); }
function fillTuning(e){
  const sz = e.sizing || {}, cr = e.criteria || {};
  const uc = sz.max_pct_per_underlying;
  _setIf("tn-uc", uc!=null ? (uc*100).toFixed(uc*100 % 1 ? 1 : 0) : "0");
  _setIf("tn-em", cr.min_strike_expected_moves);
  _setIf("tn-vrp", cr.min_iv_rv_ratio);
  _setIf("tn-avoid", (cr.avoid_setups||[]).join(", "));
  _setIf("tn-require", (cr.require_setups||[]).join(", "));
  if(document.activeElement !== $("tn-prefer-setups")) $("tn-prefer-setups").checked = !!e.prefer_setups;
  _setIf("tn-dmin", cr.delta_min);
  _setIf("tn-dmax", cr.delta_max);
  if(document.activeElement !== $("tn-ivrank")) $("tn-ivrank").checked = !!e.prefer_iv_rank;
  renderPerTicker(e.per_ticker || {});
}
function renderPerTicker(map){
  const keys = Object.keys(map || {});
  $("tn-pt-cur").textContent = keys.length ? "("+keys.length+")" : "";
  $("tn-pt-list").innerHTML = keys.length
    ? keys.map(k => `<div><b>${esc(k)}</b>: ${esc(JSON.stringify(map[k]))}</div>`).join("")
    : `<span class="muted" style="font-size:12px">No per-ticker overrides yet.</span>`;
}
function _numOrNull(id){ const r=($(id).value||"").trim(); if(r==="") return null; const v=parseFloat(r); return isNaN(v)?null:v; }
async function saveMulti(){
  const raw=($("tn-uc").value||"").trim(); const v=parseFloat(raw);
  if(raw!=="" && (isNaN(v) || v<0 || v>50)){ say("tn-uc-say","Enter 0–50 (% of account).",false); return; }
  const val = (!raw || v<=0) ? null : v/100;
  try { await postConfig({entry:{sizing:{max_pct_per_underlying: val}}});
    say("tn-uc-say", val ? ("Up to "+v+"% per ticker — multi-CSP on.") : "Off — one CSP per name.", true);
  } catch(e){ say("tn-uc-say","Couldn't save: "+e.message,false); }
}
async function saveGates(){
  try { await postConfig({entry:{criteria:{min_strike_expected_moves:_numOrNull("tn-em"), min_iv_rv_ratio:_numOrNull("tn-vrp")}}});
    say("tn-gates-say","Gates saved.",true);
  } catch(e){ say("tn-gates-say","Couldn't save: "+e.message,false); }
}
function _labels(id){
  const r=($(id).value||"").trim(); if(!r) return null;
  const L=r.split(",").map(s=>s.trim().toLowerCase()).filter(Boolean); return L.length?L:null;
}
async function saveSetupGates(){
  try { await postConfig({entry:{prefer_setups:$("tn-prefer-setups").checked,
                                 criteria:{avoid_setups:_labels("tn-avoid"), require_setups:_labels("tn-require")}}});
    say("tn-setups-say","Setup gates saved.",true);
  } catch(e){ say("tn-setups-say","Couldn't save: "+e.message,false); }
}
async function saveGlobal(){
  const dmin=_numOrNull("tn-dmin"), dmax=_numOrNull("tn-dmax");
  if(dmin==null || dmax==null || dmin<=0 || dmax<=0 || dmin>=dmax){ say("tn-global-say","Enter a valid delta band (min < max).",false); return; }
  try { await postConfig({entry:{prefer_iv_rank:$("tn-ivrank").checked, criteria:{delta_min:dmin, delta_max:dmax}}});
    say("tn-global-say","Saved.",true);
  } catch(e){ say("tn-global-say","Couldn't save: "+e.message,false); }
}
async function savePerTicker(){
  const sym=($("tn-pt-sym").value||"").trim().toUpperCase();
  if(!/^[A-Z][A-Z.]{0,5}$/.test(sym)){ say("tn-pt-say","Enter a valid symbol.",false); return; }
  const ov={};
  const dmax=_numOrNull("tn-pt-dmax"); if(dmax!=null) ov.delta_max=dmax;
  const ivr=_numOrNull("tn-pt-ivr"); if(ivr!=null) ov.min_iv_rank=ivr;
  const vrp=_numOrNull("tn-pt-vrp"); if(vrp!=null) ov.min_iv_rv_ratio=vrp;
  const em=_numOrNull("tn-pt-em"); if(em!=null) ov.min_strike_expected_moves=em;
  if(!Object.keys(ov).length){ say("tn-pt-say","Fill at least one field to override.",false); return; }
  try { const j = await postConfig({entry:{per_ticker:{[sym]:ov}}});
    renderPerTicker((j.editable.entry||{}).per_ticker||{});
    say("tn-pt-say","Saved overrides for "+sym+".",true);
    ["tn-pt-sym","tn-pt-dmax","tn-pt-ivr","tn-pt-vrp","tn-pt-em"].forEach(id=>$(id).value="");
  } catch(e){ say("tn-pt-say","Couldn't save: "+e.message,false); }
}
function renderWatchlist(){
  $("wl-count").textContent = watchlist.length ? "("+watchlist.length+")" : "";
  $("wl").innerHTML = watchlist.map(t =>
    `<span class="wl-tag">${esc(t)} <button class="wl-x" data-t="${esc(t)}" title="Remove ${esc(t)}">×</button></span>`
  ).join("") || `<span class="muted" style="font-size:12.5px">No tickers yet — add a few below.</span>`;
  $("wl").querySelectorAll(".wl-x").forEach(b => b.onclick = () => removeTicker(b.dataset.t));
}
function say(id, msg, ok){ const e=$(id); e.textContent=msg; e.className="say "+(ok?"ok":"err"); if(ok) setTimeout(()=>{if(e.textContent===msg)e.textContent="";},4000); }
async function postConfig(patch){
  const r = await fetch("/api/config",{method:"POST",headers:{"Content-Type":"application/json"},
    credentials:"same-origin",body:JSON.stringify(patch)});
  const j = await r.json().catch(()=>({}));
  if(!r.ok || !j.ok) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}
async function saveWatchlist(next, okmsg){
  try { const j = await postConfig({entry:{watchlist:next}});
    watchlist = (j.editable.entry.watchlist||[]).slice(); renderWatchlist(); say("wl-say", okmsg, true);
  } catch(e){ say("wl-say", "Couldn't save: "+e.message, false); }
}
function addTicker(){
  const inp=$("wl-in"); let t=(inp.value||"").trim().toUpperCase();
  if(!t) return;
  if(!/^[A-Z][A-Z.]{0,5}$/.test(t)){ say("wl-say","'"+t+"' doesn't look like a ticker.",false); return; }
  if(watchlist.includes(t)){ say("wl-say", t+" is already on the list.",false); inp.value=""; return; }
  const next=watchlist.concat([t]);
  inp.value="";
  saveWatchlist(next, "Added "+t+"."+(next.length>25?" (that's a lot — 10–25 is the sweet spot.)":""));
}
function removeTicker(t){ saveWatchlist(watchlist.filter(x=>x!==t), "Removed "+t+"."); }
async function saveWeekly(){
  const raw=($("wk-in").value||"").trim(); const v=parseFloat(raw);
  if(isNaN(v) || v<0 || v>100){ say("wk-say","Enter a percent between 0 and 100.",false); return; }
  try { await postConfig({entry:{weekly_premium_target_pct: v/100}}); say("wk-say", v>0?("Target set to "+v+"% of account."):"Weekly target turned off.", true); }
  catch(e){ say("wk-say","Couldn't save: "+e.message, false); }
}

/* ---- detailed tables (unchanged data, collapsed) ---- */
async function loadTables(){
  const s = await getJSON("/api/stats");
  $("rules").querySelector("tbody").innerHTML = (s.by_rule||[]).map(r => `<tr>
    <td>${esc(r.rule)}</td><td class="num">${r.closes}</td><td class="num">${r.wins}</td>
    <td class="num">${r.closes?pct(r.wins/r.closes):"—"}</td>
    <td class="num ${cls(r.realized_pnl)}">${money(r.realized_pnl)}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">no closed positions yet</td></tr>`;
  const {positions} = await getJSON("/api/positions");
  $("positions").querySelector("tbody").innerHTML = positions.map(p => {
    const pnl = p.realized_pnl!=null?p.realized_pnl:p.unrealized_pnl;
    return `<tr><td title="${esc(p.occ_symbol)}">${esc(p.underlying)} ${p.strike}${esc(p.option_type[0])}</td>
      <td class="muted">${esc(p.strategy.replace(/_/g," ").toLowerCase())}</td><td>${esc(p.status)}</td>
      <td class="num">${p.quantity}</td><td class="num">${money(p.credit_received*100*p.quantity)}</td>
      <td class="num">${p.close_price!=null?money(p.close_price*100*p.quantity):"—"}</td>
      <td class="num ${cls(pnl)}">${money(pnl)}</td><td class="num">${p.dte}</td>
      <td><span class="tag ${esc(p.outcome)}">${esc(p.outcome)}</span></td>
      <td class="muted">${esc(p.rule)||"—"}</td></tr>`; }).join("")
    || `<tr><td colspan="10" class="muted">no positions</td></tr>`;
  const {decisions} = await getJSON("/api/decisions?limit=100");
  $("decisions").querySelector("tbody").innerHTML = decisions.map(d => `<tr>
    <td class="muted">${esc(new Date(d.created_at).toLocaleString())}</td><td>${esc(d.rule_name)}</td>
    <td class="reason">${esc(d.reason)}</td><td>${d.requires_approval?"required":"auto"}</td>
    <td>${esc(d.status)}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">no decisions yet</td></tr>`;
  const {candidates} = await getJSON("/api/candidates");
  $("candidates").querySelector("tbody").innerHTML = candidates.map(c => `<tr>
    <td>${esc(c.underlying)} ${c.strike}P</td><td class="num">${c.strike}</td><td class="num">${c.dte}</td>
    <td class="num">${c.delta!=null?Math.abs(c.delta).toFixed(2):"—"}</td><td class="num">${money(c.premium)}</td>
    <td class="num">${c.annualized_ror!=null?c.annualized_ror.toFixed(0)+"%":"—"}</td>
    <td class="num">${c.open_interest!=null?c.open_interest:"—"}</td>
    <td class="num">${c.iv_rank!=null?c.iv_rank.toFixed(0):"—"}</td></tr>`).join("")
    || `<tr><td colspan="8" class="muted">no scan yet</td></tr>`;
  const {holdings} = await getJSON("/api/holdings");
  $("holdings").querySelector("tbody").innerHTML = holdings.map(h => {
    const c = h.clock || {};
    const note = h.reserve ? '<span class="pill fav">tax reserve</span>'
      : (c.days_held != null ? (c.below_basis_allowed ? `<span class="pill mixed">stuck ${c.days_held}d · calls below basis allowed</span>`
         : `<span class="pill">held ${c.days_held}d${c.under_water ? " · under water" : ""}${c.clock_days ? " · clock " + c.clock_days + "d" : ""}</span>`) : "");
    return `<tr><td>${esc(h.symbol)}</td><td class="num">${h.shares}</td><td class="num">${money(h.average_cost)}</td>
    <td class="num">${h.coverable}</td><td class="num">${h.covered}/${h.coverable}</td><td>${note}</td></tr>`; }).join("")
    || `<tr><td colspan="6" class="muted">no share holdings</td></tr>`;
  const cc = await getJSON("/api/cc-candidates");
  $("cc-candidates").querySelector("tbody").innerHTML = cc.candidates.map(c => `<tr>
    <td>${esc(c.underlying)} ${c.strike}C</td><td class="num">${c.strike}</td><td class="num">${c.dte}</td>
    <td class="num">${c.delta!=null?Math.abs(c.delta).toFixed(2):"—"}</td><td class="num">${money(c.premium)}</td>
    <td class="num">${c.annualized_ror!=null?c.annualized_ror.toFixed(0)+"%":"—"}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">no CC candidates</td></tr>`;
  const {entries} = await getJSON("/api/entry-decisions?limit=100");
  $("entries").querySelector("tbody").innerHTML = entries.map(d => `<tr>
    <td class="muted">${esc(new Date(d.created_at).toLocaleString())}</td>
    <td>${esc(d.underlying)} ${d.strike}P</td><td class="num">${d.contracts}</td><td class="num">${d.strike}</td>
    <td class="num">${money(d.premium)}</td><td>${esc(d.status)}</td></tr>`).join("")
    || `<tr><td colspan="6" class="muted">no entries yet</td></tr>`;
  const {trades} = await getJSON("/api/journal?limit=200");
  $("journal").querySelector("tbody").innerHTML = trades.map(j => `<tr>
    <td class="muted">${esc(new Date(j.entered_at).toLocaleDateString())}</td><td>${esc(j.kind)}</td>
    <td>${esc(j.underlying)} ${j.strike}${j.kind==="CC"?"C":"P"}</td>
    <td class="num">${j.delta!=null?Math.abs(j.delta).toFixed(2):"—"}</td><td class="num">${j.dte}</td>
    <td class="num">${j.iv!=null?(j.iv*100).toFixed(0)+"%":"—"}</td><td class="num">${money(j.premium)}</td>
    <td><span class="tag ${j.status==="win"||j.status==="expired"?"win":(j.status==="loss"?"loss":(j.status==="open"?"open":"assigned"))}">${esc(j.status)}</span></td>
    <td class="num ${cls(j.realized_pnl)}">${money(j.realized_pnl)}</td></tr>`).join("")
    || `<tr><td colspan="9" class="muted">no journaled trades yet</td></tr>`;
}

async function loadSetups(){
  const d = await getJSON("/api/setups");
  const tb = $("setups-tbl").querySelector("tbody");
  const note = $("setups-note");
  if(!d.enabled){
    if(note) note.textContent = "off";
    tb.innerHTML = `<tr><td colspan="11" class="muted">Setup detection is off (entry.setups.enabled).</td></tr>`;
    return;
  }
  const c = d.counts || {};
  if(note) note.textContent = (c.favorable||0)+" favorable · "+(c.avoid||0)+" avoid · "+(c.mixed||0)+" mixed";
  const rows = d.symbols || [];
  if(!rows.length){
    tb.innerHTML = `<tr><td colspan="11" class="muted">Waiting for the next scan.</td></tr>`;
    return;
  }
  const n1 = (x,dp)=> x!=null ? Number(x).toFixed(dp==null?1:dp) : "—";
  const AV = ["breakdown_confirmed","support_break","breakdown","falling_knife","breakout_from_base",
              "breakout_followthrough","climax_breakout","breakout_confirmed","breakout_strong","breakout"];
  const FV = ["support_test_rejection","support_test_on_volume","quiet_base","washout_at_support","washout"];
  const lblChip = (l)=> `<span class="lbl ${AV.includes(l)?"avoid":(FV.includes(l)?"fav":"")}">${esc(human(l))}</span>`;
  tb.innerHTML = rows.map(r=>{
    const f = r.features||{}, lv = r.live||{};
    const live = lv.breakout_attempt ? '<span class="pill up">breakout attempt</span>'
               : lv.breakdown_attempt ? '<span class="pill live">breakdown attempt</span>'
               : lv.support_break_attempt ? '<span class="pill live">support break</span>'
               : (r.partial_bar ? '<span class="pill">quiet</span>' : "—");
    const sup = f.support_ref!=null ? n1(f.support_ref,2)+(f.dist_to_support_pct!=null ? " ("+n1(f.dist_to_support_pct)+"%)" : "") : "—";
    const res = f.resistance_ref!=null ? n1(f.resistance_ref,2)+(f.dist_to_resistance_pct!=null ? " ("+n1(f.dist_to_resistance_pct)+"%)" : "") : "—";
    const gate = (r.gate && r.gate.blocked) ? ` <span class="tvstale" title="${esc(r.gate.reason||"")}">gated</span>` : "";
    const tvb = (r.tv && r.tv.present) ? ` <span class="count" title="TradingView flags merged: ${esc((r.tv.sources||[]).join(", ")||"context")}">TV</span>` : "";
    return `<tr><td><b>${esc(r.symbol)}</b>${gate}${tvb}</td><td class="num">${r.price!=null?money(r.price):"—"}</td>
      <td style="white-space:normal;min-width:160px">${(r.setups||[]).map(lblChip).join("") || '<span class="muted">none</span>'}</td>
      <td>${biasPill(r.bias)}</td><td>${live}</td>
      <td class="num">${n1(f.rsi,0)}</td><td class="num">${n1(f.bb_percent_b,0)}</td><td class="num">${n1(f.bb_width_pct)}</td>
      <td class="num">${f.vol_ratio_20!=null ? n1(f.vol_ratio_20)+"×" : "n/a"}</td>
      <td class="num">${sup}</td><td class="num">${res}</td></tr>`;
  }).join("");
}
async function loadSetupAccuracy(){
  const d = await getJSON("/api/setups/accuracy");
  const tb = $("setups-acc-tbl").querySelector("tbody");
  const note = $("setups-acc-note");
  const rows = (d.rows||[]);
  if(!d.available || !rows.length){
    if(note) note.textContent = d.available ? "no resolved fires yet" : "tracker off";
    return;
  }
  const resolved = rows.reduce((a,r)=>a+(r.n||0),0), pend = rows.reduce((a,r)=>a+(r.n_pending||0),0);
  if(note) note.textContent = resolved+" resolved · "+pend+" pending";
  const pc = (v)=> v!=null ? (v*100).toFixed(1)+"%" : "—";
  const hr = (v)=> v!=null ? (v*100).toFixed(0)+"%" : "—";
  tb.innerHTML = rows.map(r=>{
    const ne = (r.n_ep!=null) ? r.n_ep : r.n;              // episodes = the honest count
    const weak = (ne||0) < 15;
    return `<tr class="${weak?"muted":""}"><td>${esc(human(r.label))}${r.source && r.source!=="bot_daily" ? ' <span class="muted">('+esc(r.source)+')</span>' : ""}</td>
      <td>${biasPill(r.bias)}</td><td class="num">${r.n}</td><td class="num"><b>${ne!=null?ne:"—"}</b></td><td class="num">${hr(r.hit_rate_5d)}</td>
      <td class="num ${cls(r.avg_ret_5d)}">${pc(r.avg_ret_5d)}</td><td class="num ${cls(r.avg_ret_10d)}">${pc(r.avg_ret_10d)}</td>
      <td class="num neg">${pc(r.avg_mae_10d)}</td><td class="num">${r.n_pending||0}</td></tr>`;
  }).join("");
}
let riskProfile = null;
async function loadRiskProfile(){
  const d = await getJSON("/api/risk-profile");
  riskProfile = d;
  const c = d.config || {};
  if($("rp-base")) $("rp-base").textContent = c.base_cushion; if($("rp-h")) $("rp-h").textContent = c.horizon;
  if($("rp-target")) $("rp-target").textContent = c.target_itm_rate!=null ? (c.target_itm_rate*100).toFixed(0)+"%" : "";
  const tb = $("rp-tbl").querySelector("tbody");
  const rows = d.profiles || [];
  if($("rp-note")) $("rp-note").textContent = rows.length ? rows.length+" names · "+(d.proposals||[]).length+" suggestions" : "";
  if(!rows.length){ tb.innerHTML = `<tr><td colspan="7" class="muted">Profiles compute on each name's first scan of the day.</td></tr>`; }
  else {
    const pc = (v)=> v!=null ? (v*100).toFixed(1)+"%" : "—";
    tb.innerHTML = rows.map(p=>{
      const note = !p.reliable ? '<span class="pill">thin history</span>' : (p.needs_tightening ? '<span class="pill mixed">wider cushion</span>' : '<span class="pill fav">ok at base</span>');
      return `<tr><td><b>${esc(p.symbol||"")}</b></td><td class="num">${p.n}</td>
        <td class="num">${pc(p.touch_rate)}</td><td class="num">${pc(p.itm_rate)}</td><td class="num neg">${pc(p.avg_worst_pct)}</td>
        <td class="num">${p.suggested_cushion!=null ? p.suggested_cushion+"σ" : "—"}</td><td>${note}</td></tr>`;
    }).join("");
  }
  const props = d.proposals || [];
  if($("tn-cush-note")) $("tn-cush-note").textContent = props.length ? "("+props.length+" suggested)" : "";
  if($("tn-cush-list")) $("tn-cush-list").innerHTML = props.length
    ? props.map(p=>`<div><b>${esc(p.symbol)}</b>: ${p.current!=null?p.current:"none"} → <b>${p.proposed}σ</b> <span class="muted">— ${esc(p.reason)}</span></div>`).join("")
    : `<span class="muted" style="font-size:12px">No names need a wider cushion right now.</span>`;
}
async function applyPutSellerPreset(){
  try {
    const d = riskProfile || await getJSON("/api/risk-profile");
    const preset = ((d.config||{}).put_seller_avoid_preset)||[];
    if(!preset.length){ say("tn-setups-say","Preset unavailable.",false); return; }
    const cur = _labels("tn-avoid") || [];
    const merged = Array.from(new Set(cur.concat(preset)));
    $("tn-avoid").value = merged.join(", ");
    await saveSetupGates();
  } catch(e){ say("tn-setups-say","Couldn't apply preset: "+e.message,false); }
}
async function applySuggestedCushions(){
  try {
    const d = riskProfile || await getJSON("/api/risk-profile");
    const props = d.proposals || [];
    if(!props.length){ say("tn-cush-say","Nothing to apply — no name needs a wider cushion.",true); return; }
    const per = {};
    props.forEach(p => { per[p.symbol] = {min_strike_expected_moves: p.proposed}; });
    await postConfig({entry:{per_ticker: per}});
    say("tn-cush-say","Applied wider cushions for "+props.map(p=>p.symbol).join(", ")+".",true);
    await loadControls(); await loadRiskProfile();
  } catch(e){ say("tn-cush-say","Couldn't apply: "+e.message,false); }
}
async function loadQuality(){
  const d = await getJSON("/api/quality");
  const tb = $("quality-tbl").querySelector("tbody");
  const note = $("q-note");
  if(!d.enabled){
    if(note) note.textContent = "off";
    tb.innerHTML = `<tr><td colspan="9" class="muted">Company quality scoring is off — set entry.quality_scoring: true (informational; does not affect trading).</td></tr>`;
    return;
  }
  if(note) note.textContent = "informational — not a trade input";
  const syms = d.symbols || {}; const keys = Object.keys(syms).sort();
  if(!keys.length){
    tb.innerHTML = `<tr><td colspan="9" class="muted">No quality data yet — waiting for the next scan.</td></tr>`;
    return;
  }
  const pct = (x)=> x!=null ? (x*100).toFixed(0)+"%" : "—";
  const sc  = (x)=> x!=null ? Number(x).toFixed(0) : "—";
  tb.innerHTML = keys.map(k=>{ const q = syms[k]||{}; return `<tr>
    <td>${esc(k)}</td><td class="muted">${esc(q.sector)||"—"}</td>
    <td class="num"><b>${sc(q.score)}</b></td>
    <td class="num">${pct(q.gross_margin)}</td><td class="num">${pct(q.net_margin)}</td>
    <td class="num">${pct(q.revenue_growth)}</td><td class="num">${pct(q.fcf_margin)}</td>
    <td class="num">${q.gross_profitability!=null?Number(q.gross_profitability).toFixed(2):"—"}</td>
    <td class="num">${q.insider_net_buys_90d!=null?q.insider_net_buys_90d:"—"}</td></tr>`; }).join("");
}

function mdLite(s){
  const esc2=(t)=>t.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
  const bold=(t)=>t.replace(/\\*\\*(.+?)\\*\\*/g,"<b>$1</b>");
  return esc2(s).split("\\n").map(line=>{
    if(line.startsWith("### ")) return "<h4 style='margin:10px 0 4px'>"+bold(line.slice(4))+"</h4>";
    if(line.startsWith("## "))  return "<h3 style='margin:14px 0 6px'>"+bold(line.slice(3))+"</h3>";
    if(line.startsWith("# "))   return "<h2 style='margin:4px 0 8px'>"+bold(line.slice(2))+"</h2>";
    if(line.startsWith("- "))   return "<div style='margin-left:1em'>&bull; "+bold(line.slice(2))+"</div>";
    if(line.startsWith("> "))   return "<div class='muted' style='font-style:italic'>"+bold(line.slice(2))+"</div>";
    if(line.startsWith("_") && line.endsWith("_")) return "<div class='muted' style='font-style:italic'>"+bold(line.slice(1,-1))+"</div>";
    if(line.trim()==="") return "<div style='height:6px'></div>";
    return "<div>"+bold(line)+"</div>";
  }).join("");
}
let briefShown = null;   // id of the brief currently on screen (null = none yet)
function briefWhen(iso){
  if(!iso) return "";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})+" "+d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});
}
function showBrief(d, note){
  const el = $("brief-body"), meta = $("brief-meta");
  el.classList.remove("muted");
  el.innerHTML = mdLite(d.body||"");
  briefShown = d.id || "unsaved";
  const m = d.meta || {};
  meta.innerHTML = [
    d.created_at ? `<span>Generated <b>${esc(briefWhen(d.created_at))}</b></span>` : "",
    `<span>${d.has_ai ? "AI tactical read included" : "Deterministic read (no AI)"}</span>`,
    m.open_positions!=null ? `<span><b>${m.open_positions}</b> open at the time</span>` : "",
    note ? `<span class="muted">${esc(note)}</span>` : "",
  ].filter(Boolean).join("");
  const sel = $("brief-list"); if(sel && d.id) sel.value = d.id;
}
async function generateBrief(){
  const el = $("brief-body"), btn = $("brief-run");
  el.textContent = "Building brief… (reading your accounts live — a few seconds)";
  btn.disabled = true;
  try { const d = await getJSON("/api/brief"); await loadBriefList(); showBrief(d, d.id ? "saved" : "not saved"); }
  catch(e){ el.textContent = "Failed to build brief: "+e.message; }
  finally { btn.disabled = false; }
}
async function loadBriefList(){
  const sel = $("brief-list"); if(!sel) return;
  const d = await getJSON("/api/briefs?limit=40");
  if(!d.available){ sel.hidden = true; return; }
  const rows = d.briefs || [];
  const cur = sel.value;
  sel.innerHTML = `<option value="">${rows.length ? rows.length+" saved brief"+(rows.length===1?"":"s") : "No saved briefs yet"}</option>`
    + rows.map(b => `<option value="${esc(b.id)}">${esc(briefWhen(b.created_at))}${b.has_ai?" · AI":""}</option>`).join("");
  if(cur && rows.some(b=>b.id===cur)) sel.value = cur;
  // First load: open on the latest saved brief instead of an empty pane.
  if(briefShown === null && d.latest) showBrief(d.latest, "latest saved");
}
async function openSavedBrief(id){
  if(!id) return;
  try { const d = await getJSON("/api/briefs/"+encodeURIComponent(id)); showBrief(d, "saved"); }
  catch(e){ $("brief-meta").textContent = "Couldn't load that brief: "+e.message; }
}

/* ---- orchestration ---- */
let lastLoad = 0;
async function loadAll(){
  for (const [fn,name] of [[loadConn,"conn"],[loadHolds,"holds"],[loadWeek,"week"],
                           [loadFeed,"feed"],[loadScanStatus,"scan"],[loadTvLevels,"tv"],
                           [loadQuality,"quality"],[loadSetups,"setups"],[loadSetupAccuracy,"setupacc"],
                           [loadRiskProfile,"riskprofile"],[loadRegimeStatus,"regime"],
                           [loadReserve,"reserve"],[loadTiers,"tiers"],[loadTables,"tables"]]) {
    try { await fn(); } catch(e){ console.error(name, e); }
  }
  lastLoad = Date.now();
}
function tickAgo(){
  if(!lastLoad){ return; }
  const s = Math.round((Date.now()-lastLoad)/1000);
  $("ago").textContent = s < 5 ? "Updated just now" : "Updated " + s + "s ago";
}
$("brief-run").onclick = generateBrief;
$("brief-list").onchange = (e) => openSavedBrief(e.target.value);
loadBriefList().catch(e=>console.error("briefs",e));
$("scr-run").onclick = runScreen;
$("scr-scan").onclick = runScan;
$("wl-add").onclick = addTicker;
$("wl-in").addEventListener("keydown", e => { if(e.key==="Enter") addTicker(); });
$("wk-save").onclick = saveWeekly;
$("wk-in").addEventListener("keydown", e => { if(e.key==="Enter") saveWeekly(); });
$("tn-uc-save").onclick = saveMulti;
$("tn-dt-save").onclick = saveDowntrend;
$("tn-tr-save").onclick = saveReserve;
$("tn-gates-save").onclick = saveGates;
$("tn-global-save").onclick = saveGlobal;
$("tn-pt-save").onclick = savePerTicker;
$("tn-setups-save").onclick = saveSetupGates;
$("tn-preset-putseller").onclick = applyPutSellerPreset;
$("tn-cush-apply").onclick = applySuggestedCushions;

/* ---- collapsible cards: tap a card title to fold it; remembered per device ---- */
(function initCollapsibles(){
  document.querySelectorAll(".card2").forEach(card => {
    const h = card.querySelector(":scope > .card-h"); const t = h && h.querySelector("h2");
    if(!h || !t || card.dataset.nofold) return;
    const key = "tc_fold_" + t.textContent.trim().toLowerCase().split(" ").join("-").slice(0,40);
    h.classList.add("clp"); t.insertAdjacentHTML("afterbegin", '<span class="chev"></span>');
    let closed = card.dataset.fold === "closed";
    try { const v = localStorage.getItem(key); if(v !== null) closed = (v === "1"); } catch(e){}
    card.classList.toggle("collapsed", closed);
    h.addEventListener("click", (e) => {
      if(e.target.closest("button,a,select,input,label")) return;
      const now = !card.classList.contains("collapsed");
      card.classList.toggle("collapsed", now);
      try { localStorage.setItem(key, now ? "1" : "0"); } catch(e){}
    });
  });
})();

/* ---- tab navigation ---- */
function showTab(name){
  document.querySelectorAll(".tabpane").forEach(p => { p.hidden = (p.id !== "pane-"+name); });
  document.querySelectorAll(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab===name));
  try { localStorage.setItem("tc_tab", name); } catch(e){}
  window.scrollTo({top:0});
}
document.querySelectorAll(".tab-btn").forEach(b => b.onclick = () => showTab(b.dataset.tab));
(function(){ let t = "overview"; try { t = localStorage.getItem("tc_tab") || "overview"; } catch(e){}
  if(!document.getElementById("pane-"+t)) t = "overview"; showTab(t); })();

loadControls().catch(e=>console.error("controls",e));
loadAll();
setInterval(loadAll, 25000);   // auto-refresh data (controls are loaded on demand, never clobbered)
setInterval(tickAgo, 1000);
</script>
</body>
</html>"""
