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

from typing import TYPE_CHECKING

from fastapi import APIRouter, Body, Depends
from fastapi.responses import HTMLResponse, PlainTextResponse

from ..services.stats import compute_stats, position_rows
from .auth import require_auth
from .calc_page import CALC_PAGE

if TYPE_CHECKING:
    from .app import WebDeps


def make_dashboard_router(deps: "WebDeps") -> APIRouter:
    # All dashboard + data routes require auth (when a password is configured).
    router = APIRouter(dependencies=[Depends(require_auth)])

    @router.get("/api/stats")
    async def api_stats() -> dict:
        # Live mode: show only real trades (exclude leftover paper-soak history).
        return compute_stats(
            deps.positions.list_all(), deps.orders.list_all(), deps.decisions.recent(1000),
            real_only=deps.settings.is_live,
        )

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
            "regime": reg.as_dict() if reg is not None else None,
        }

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
        covered: dict[str, int] = {}
        for p in deps.positions.list_open():
            if p.direction is Direction.SHORT and p.option_type is OptionType.CALL:
                covered[p.underlying] = covered.get(p.underlying, 0) + p.quantity
        return {"holdings": [{
            "symbol": h.symbol, "shares": h.quantity, "average_cost": h.average_cost,
            "coverable": h.quantity // 100, "covered": covered.get(h.symbol, 0),
        } for h in holds]}

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
  .card2{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
  .card-h{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:14px 16px 0}
  .card-h h2{font-size:13px;font-weight:650;margin:0}
  .count{font-size:12px;color:var(--faint);font-weight:600}
  /* position card */
  .pos-item{padding:16px} .pos-item + .pos-item{border-top:1px solid var(--line)}
  .pos-top{display:flex;align-items:flex-start;justify-content:space-between;gap:12px}
  .tick{font-size:17px;font-weight:700;letter-spacing:-.01em}
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
  /* details */
  details.more{background:var(--card);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--shadow)}
  details.more>summary{cursor:pointer;padding:14px 16px;font-size:13px;font-weight:650;list-style:none;display:flex;align-items:center;gap:8px}
  details.more>summary::-webkit-details-marker{display:none}
  details.more>summary::before{content:"▸";color:var(--faint)}
  details.more[open]>summary::before{content:"▾"}
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
        <div class="card-h"><h2>This week</h2><span class="count">real trades</span></div>
        <div id="week"></div>
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

      <section class="card2">
        <div class="card-h"><h2>TradingView levels</h2><span class="count" id="tv-last"></span></div>
        <div class="ctl" id="tv-levels"></div>
      </section>
    </div>
  </div>

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

  <details class="more">
    <summary>Detailed data — opportunities, holdings, entries, journal, all decisions</summary>
    <div class="more-body">
      <h3>Per-rule performance</h3>
      <table id="rules"><thead><tr><th>Rule</th><th class="num">Closes</th><th class="num">Wins</th>
        <th class="num">Win&nbsp;%</th><th class="num">Realized&nbsp;P&amp;L</th></tr></thead><tbody></tbody></table>
      <h3>All positions</h3>
      <table id="positions"><thead><tr><th>Symbol</th><th>Strategy</th><th>Status</th><th class="num">Qty</th>
        <th class="num">Credit</th><th class="num">Close</th><th class="num">P&amp;L</th><th class="num">DTE</th>
        <th>Outcome</th><th>Rule</th></tr></thead><tbody></tbody></table>
      <h3>Decision log — the "why"</h3>
      <table id="decisions"><thead><tr><th>When</th><th>Rule</th><th>Reason</th><th>Approval</th><th>Status</th>
        </tr></thead><tbody></tbody></table>
      <h3>Opportunities — latest CSP scan</h3>
      <table id="candidates"><thead><tr><th>Symbol</th><th class="num">Strike</th><th class="num">DTE</th>
        <th class="num">&Delta;</th><th class="num">Premium</th><th class="num">Ann&nbsp;%</th>
        <th class="num">OI</th><th class="num">IVR</th></tr></thead><tbody></tbody></table>
      <h3>Holdings — shares &amp; covered-call coverage</h3>
      <table id="holdings"><thead><tr><th>Symbol</th><th class="num">Shares</th><th class="num">Cost basis</th>
        <th class="num">Coverable</th><th class="num">Covered</th></tr></thead><tbody></tbody></table>
      <h3>CC opportunities — calls on shares you hold</h3>
      <table id="cc-candidates"><thead><tr><th>Symbol</th><th class="num">Strike</th><th class="num">DTE</th>
        <th class="num">&Delta;</th><th class="num">Premium</th><th class="num">Ann&nbsp;%</th></tr></thead><tbody></tbody></table>
      <h3>Entry log — auto-entered CSPs &amp; CCs</h3>
      <table id="entries"><thead><tr><th>When</th><th>Symbol</th><th class="num">Qty</th><th class="num">Strike</th>
        <th class="num">Premium</th><th>Status</th></tr></thead><tbody></tbody></table>
      <h3>Trade journal — labeled entries + outcomes (learning data)</h3>
      <table id="journal"><thead><tr><th>Entered</th><th>Kind</th><th>Symbol</th><th class="num">&Delta;</th>
        <th class="num">DTE</th><th class="num">IV</th><th class="num">Premium</th><th>Status</th>
        <th class="num">P&amp;L</th></tr></thead><tbody></tbody></table>
    </div>
  </details>

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
async function getJSON(u){ const r = await fetch(u,{credentials:"same-origin"}); if(!r.ok) throw new Error(u+" -> "+r.status); return r.json(); }

/* ---- connection hero ---- */
async function loadConn(){
  const top = $("conn-top"), note = $("conn-note");
  let st = {}, bs = {};
  try { st = await getJSON("/control/status"); } catch(e){
    top.innerHTML = `<span class="conn bad"><span class="dot"></span> Can't reach the bot</span>`;
    note.innerHTML = `The dashboard couldn't load the bot's status. It may be redeploying — try again in a minute.`; return;
  }
  try { bs = await getJSON("/control/broker-status"); } catch(e){ bs = {ok:false}; }
  const paper = bs.is_paper === true, live = st.mode === "live";
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
      ${bs.buying_power!=null?`<span class="chip">Buying power <b>${money(bs.buying_power)}</b></span>`:""}
      ${bs.open_positions!=null?`<span class="chip">${bs.open_positions} open</span>`:""}
    </div>`;
  note.innerHTML = (klass==="bad")
    ? `<b style="color:var(--neg)">Heads up:</b> the bot is not managing your real Robinhood positions right now.`
    : `This banner turns <b>red</b> the moment the bot loses its Robinhood connection or drops to the simulator.`;
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
        <div><div class="tick">${esc(p.underlying)} <span class="kind">$${p.strike} ${put?"put":"call"} · ${esc(p.strategy.replace(/_/g," ").toLowerCase())}</span></div>
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
  $("week").innerHTML = `
    <div class="stat"><span class="k">Realized P&L</span><span class="v ${cls(s.realized_pnl)}">${money(s.realized_pnl)}</span></div>
    <div class="stat"><span class="k">Win rate</span><span class="v">${pct(s.win_rate)} <span class="badge">${s.wins} / ${s.resolved_count}</span></span></div>
    <div class="stat"><span class="k">Open now</span><span class="v">${s.open_count}</span></div>
    <div class="stat"><span class="k">Unrealized</span><span class="v ${cls(s.unrealized_pnl)}">${money(s.unrealized_pnl)}</span></div>`;
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
  watchlist = (cfg.editable.entry.watchlist || []).slice();
  renderWatchlist();
  const wt = cfg.editable.entry.weekly_premium_target_pct;
  if(document.activeElement !== $("wk-in")) $("wk-in").value = wt ? (wt*100).toFixed(wt*100 % 1 ? 1 : 0) : "0";
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
  $("holdings").querySelector("tbody").innerHTML = holdings.map(h => `<tr>
    <td>${esc(h.symbol)}</td><td class="num">${h.shares}</td><td class="num">${money(h.average_cost)}</td>
    <td class="num">${h.coverable}</td><td class="num">${h.covered}/${h.coverable}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">no share holdings</td></tr>`;
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

/* ---- orchestration ---- */
let lastLoad = 0;
async function loadAll(){
  for (const [fn,name] of [[loadConn,"conn"],[loadHolds,"holds"],[loadWeek,"week"],
                           [loadFeed,"feed"],[loadScanStatus,"scan"],[loadTvLevels,"tv"],
                           [loadTables,"tables"]]) {
    try { await fn(); } catch(e){ console.error(name, e); }
  }
  lastLoad = Date.now();
}
function tickAgo(){
  if(!lastLoad){ return; }
  const s = Math.round((Date.now()-lastLoad)/1000);
  $("ago").textContent = s < 5 ? "Updated just now" : "Updated " + s + "s ago";
}
$("scr-run").onclick = runScreen;
$("scr-scan").onclick = runScan;
$("wl-add").onclick = addTicker;
$("wl-in").addEventListener("keydown", e => { if(e.key==="Enter") addTicker(); });
$("wk-save").onclick = saveWeekly;
$("wk-in").addEventListener("keydown", e => { if(e.key==="Enter") saveWeekly(); });
loadControls().catch(e=>console.error("controls",e));
loadAll();
setInterval(loadAll, 25000);   // auto-refresh data (controls are loaded on demand, never clobbered)
setInterval(tickAgo, 1000);
</script>
</body>
</html>"""
