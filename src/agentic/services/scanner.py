"""OpportunityScanner: the entry-side loop (universe-centric counterpart to MonitorLoop).

Each cycle (market-hours only, when entry.enabled): scan the watchlist for option chains,
screen for CSP candidates, run the risk/sizing layer against live buying power + open
positions, then auto-enter approved candidates via the executor (behind all the usual gates).

Opened positions are NOT created here — the broker reports them and reconcile discovers them,
after which the existing close-management rules take over (the wheel handoff).
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta

from ..config import Settings
from ..brokers.base import ExecutionBroker
from ..errors import describe_exception
from ..domain.enums import AuditEventType, DecisionStatus, OrderStatus
from ..domain.models import EntryDecision, TradeJournalEntry, utcnow
from ..marketdata.base import MarketDataProvider
from ..marketdata.quote import OptionContractQuote
from ..store.audit import AuditStore
from ..store.entry_decisions import EntryDecisionStore
from ..store.trade_journal import TradeJournalStore
from .risk_breaker import evaluate_risk_breaker
from .executor import OrderExecutor
from .killswitch import KillSwitch
from .market_hours import is_market_hours
from ..entry.context import UnderlyingContext, build_context, passes_underlying_gates
from ..entry.regime import MarketRegime, build_market_regime, classify_move
from ..entry.risk import RiskSizer
from ..entry.screener import EntryCandidate, screen_candidates
from ..marketdata.earnings import NullEarningsProvider, earnings_blackout

log = logging.getLogger("agentic.scanner")


def iv_rank_sort_key(candidate, context_by_underlying) -> tuple[float, float]:
    """Ranking key for ``prefer_iv_rank``: prefer richer premium (higher underlying IV rank), with
    theta-efficiency as the tiebreak. Unknown IV rank (too little history) maps to 50 — neutral, so
    names still accumulating history are never penalized. Sort descending on this tuple."""
    ctx = context_by_underlying.get(candidate.underlying)
    ivr = ctx.iv_rank if (ctx is not None and ctx.iv_rank is not None) else 50.0
    return (float(ivr), float(candidate.theta_efficiency))


class OpportunityScanner:
    def __init__(
        self,
        settings: Settings,
        broker: ExecutionBroker,
        market_data: MarketDataProvider,
        entry_decisions: EntryDecisionStore,
        executor: OrderExecutor,
        audit: AuditStore,
        killswitch: KillSwitch,
        trade_journal: TradeJournalStore | None = None,
        ai_reviewer=None,           # ai.reviewer.AIReviewer | None (loose to avoid an import cycle)
        tv_indicators=None,         # store.tv_indicators.TVIndicatorStore | None
        ai_reviews=None,            # store.ai_reviews.AIReviewStore | None
        earnings=None,              # marketdata.earnings.EarningsProvider | None
        entry_candidates=None,      # store.entry_candidates.EntryCandidateStore | None
        news_provider=None,         # marketdata.news.NewsProvider | None (pull)
        news=None,                  # store.news.NewsStore | None
    ):
        self.settings = settings
        self.broker = broker
        self.market_data = market_data
        self.entry_decisions = entry_decisions
        self.executor = executor
        self.audit = audit
        self.killswitch = killswitch
        self.trade_journal = trade_journal
        self.ai_reviewer = ai_reviewer
        self.tv_indicators = tv_indicators
        self.ai_reviews = ai_reviews
        self.entry_candidates = entry_candidates
        self.earnings = earnings or NullEarningsProvider()
        self.news_provider = news_provider
        self.news = news
        self.sizer = RiskSizer(settings.entry.sizing)
        self._stop = asyncio.Event()
        # Latest scan snapshot for the dashboard.
        self.last_candidates: list[EntryCandidate] = []       # CSP candidates
        self.last_cc_candidates: list[EntryCandidate] = []    # covered-call candidates
        self.last_holdings: list = []                         # EquityHolding snapshot
        self.last_context: dict[str, UnderlyingContext] = {}  # per-symbol technicals/IV-rank
        self.last_skips: list[dict] = []                      # names skipped by underlying gates
        self.last_error: str | None = None
        self.last_scan_at = None
        self.last_regime: MarketRegime | None = None          # market-wide regime snapshot
        self.last_breaker: dict | None = None                 # loss circuit breaker state (freezes new entries)

    async def _compute_regime(self) -> MarketRegime | None:
        """Best-effort market-regime snapshot from index-ETF daily bars. Never raises (advisory)."""
        cfg = self.settings.macro
        syms = cfg.symbols or ["SPY", "QQQ"]
        spy_sym = syms[0]
        qqq_sym = syms[1] if len(syms) > 1 else syms[0]
        try:
            spy_bars = await self.market_data.get_underlying_bars(spy_sym, cfg.lookback_days)
            qqq_bars = await self.market_data.get_underlying_bars(qqq_sym, cfg.lookback_days)
        except Exception as exc:  # noqa: BLE001 — regime is advisory; a data gap must not break the scan
            log.warning("Regime data fetch failed: %s", exc)
            return None
        return build_market_regime(spy_bars or [], qqq_bars or [], cfg)

    async def run(self) -> None:
        if not self.settings.entry.enabled:
            log.info("Entry scanner disabled (entry.enabled=false); not starting.")
            return
        log.info("Opportunity scanner started (watchlist=%d names, feed=%s).",
                 len(self.settings.entry.watchlist), self.settings.entry.feed)
        while not self._stop.is_set():
            try:
                await self.run_once()
                self.last_error = None
                self.killswitch.record_success()
            except Exception as exc:  # noqa: BLE001 — a bad scan must not kill the loop
                self.last_error = str(exc)
                log.exception("Scanner cycle error: %s", exc)
                self.audit.record(
                    AuditEventType.ERROR,
                    {"where": "scanner", **describe_exception(exc)},
                )
                self.killswitch.record_broker_error("scanner")
            delay = self.settings.entry.scan_interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def run_once(self) -> int:
        """One scan cycle: a CSP pass (watchlist) + a CC pass (shares held). Returns entries submitted."""
        cfg = self.settings.entry
        if not cfg.enabled:
            return 0
        if self.killswitch.is_paused():
            log.debug("Scanner skipped: killswitch paused.")
            return 0
        if not is_market_hours():
            return 0

        # Account state for sizing.
        buying_power = await self.broker.get_buying_power()
        account_value = await self.broker.get_account_value()

        # Loss circuit breaker: freeze NEW entries when realized losses pile up. Does NOT close
        # anything — the monitor keeps managing/closing existing positions. Evaluated fresh each
        # cycle (self-clearing as losing trades roll out of the window).
        breaker = evaluate_risk_breaker(self.trade_journal, self.settings.risk, account_value, utcnow())
        self.last_breaker = breaker
        if breaker.get("tripped"):
            log.warning("Loss circuit breaker ENGAGED — freezing new entries: %s", breaker["reason"])
            return 0

        open_positions = await self.broker.get_open_positions()
        holdings = await self.broker.get_equity_positions()
        self.last_holdings = holdings

        # Market-regime read (systemic-vs-idiosyncratic context for the AI reviewer / optional gate).
        if self.settings.macro.enabled:
            self.last_regime = await self._compute_regime()

        # Chain cache so a symbol in both the watchlist and holdings is fetched once.
        chain_cache: dict[str, list[OptionContractQuote]] = {}
        quote_by_occ: dict[str, OptionContractQuote] = {}

        async def chain_for(symbol: str) -> list[OptionContractQuote]:
            if symbol not in chain_cache:
                ch = await self.market_data.get_chain(symbol)
                chain_cache[symbol] = ch
                for c in ch:
                    quote_by_occ[c.occ_symbol] = c
            return chain_cache[symbol]

        context_by_underlying: dict[str, UnderlyingContext] = {}
        skips: list[dict] = []

        # CSP pass — watchlist puts, per-ticker criteria + underlying gates (entry intelligence).
        csp_cands: list[EntryCandidate] = []
        today = utcnow().date()
        await self._refresh_news(cfg.watchlist)
        for underlying in cfg.watchlist:
            crit = cfg.criteria_for(underlying, cfg.criteria)
            chain = await chain_for(underlying)
            ctx = await self._context_for(underlying, chain, crit)
            self._enrich_ctx_from_tv(underlying, ctx)
            ctx.recent_news_count = len(self._recent_news(underlying))
            earn_date = await self.earnings.next_earnings(underlying)
            if earn_date is not None:
                ctx.days_to_earnings = (earn_date - today).days
            context_by_underlying[underlying] = ctx
            reason = passes_underlying_gates(ctx, crit)
            if reason is None and cfg.earnings_gate and earnings_blackout(
                    earn_date, today, crit.dte_max, crit.exclude_earnings_days):
                reason = f"earnings in {ctx.days_to_earnings}d (blackout)"
            if reason is not None:
                skips.append({"symbol": underlying, "reason": reason})
                continue
            cands = screen_candidates(underlying, chain, crit)
            ceiling = self._support_ceiling(underlying, crit)
            if ceiling is not None:
                kept = [c for c in cands if c.strike <= ceiling]
                if len(kept) < len(cands):
                    skips.append({"symbol": underlying, "reason":
                                  f"{len(cands) - len(kept)} strike(s) above support {ceiling:.2f}"})
                cands = kept
            csp_cands.extend(cands)
        if cfg.prefer_iv_rank:
            csp_cands.sort(key=lambda c: iv_rank_sort_key(c, context_by_underlying), reverse=True)
        else:
            csp_cands.sort(key=lambda x: x.theta_efficiency, reverse=True)  # decay-per-collateral
        self.last_candidates = csp_cands
        csp_result = self.sizer.evaluate(
            csp_cands, buying_power=buying_power, account_value=account_value,
            open_positions=open_positions,
        )
        csp_approved = csp_result.approved
        csp_rejected = list(csp_result.rejected)

        # Optional hard regime gate (default off): pause NEW CSP entries while the market is risk-off.
        if (self.settings.macro.enabled and self.settings.macro.hard_gate
                and self.last_regime is not None and self.last_regime.risk_off):
            if csp_approved:
                log.info("Regime risk_off + hard_gate: suppressing %d CSP entr(ies).",
                         len(csp_approved))
            skips.append({"symbol": "*", "reason": f"regime risk_off ({self.last_regime.label})"})
            # Sizing approved them but the regime gate vetoes — log them as rejected, not approved.
            csp_rejected.extend(
                (e.candidate, f"regime risk_off hard_gate ({self.last_regime.label})")
                for e in csp_approved
            )
            csp_approved = []

        # CC pass — covered calls on shares held (strike floored at cost basis), per-ticker criteria.
        cc_cands: list[EntryCandidate] = []
        for h in holdings:
            if h.quantity < 100:
                continue
            crit = cfg.criteria_for(h.symbol, cfg.cc_criteria)
            chain = await chain_for(h.symbol)
            if h.symbol not in context_by_underlying:
                context_by_underlying[h.symbol] = await self._context_for(h.symbol, chain, crit)
            cc_cands.extend(screen_candidates(
                h.symbol, chain, crit, option_type="call", strike_floor=h.average_cost,
            ))
        cc_cands.sort(key=lambda x: x.theta_efficiency, reverse=True)
        self.last_cc_candidates = cc_cands
        self.last_context = context_by_underlying
        self.last_skips = skips
        cc_result = self.sizer.evaluate_covered_calls(
            cc_cands, holdings=holdings, open_positions=open_positions,
        )
        cc_approved = cc_result.approved
        self.last_scan_at = utcnow()

        # Persist the full scan disposition (approved + reasoned rejections) — the negative-example
        # dataset for refining entry logic. Best-effort: a logging failure must not break the scan.
        self._record_candidates(csp_approved, csp_rejected, cc_approved,
                                list(cc_result.rejected))

        # Seed IV history (ATM IV per scanned symbol, once/day) for later IV-Rank computation.
        if self.trade_journal is not None:
            today = utcnow().date()
            for sym, ch in chain_cache.items():
                iv = self._atm_iv(ch)
                if iv is not None:
                    self.trade_journal.record_iv(sym, today, iv)

        self.audit.record(
            AuditEventType.POLL,
            {"scan": True, "watchlist": len(cfg.watchlist), "holdings": len(holdings),
             "csp_candidates": len(csp_cands), "csp_approved": len(csp_approved),
             "cc_candidates": len(cc_cands), "cc_approved": len(cc_approved),
             "skipped": skips, "buying_power": buying_power},
            source="scanner",
        )
        log.info("Scan: CSP %d cand/%d approved; CC %d cand/%d approved.",
                 len(csp_cands), len(csp_approved), len(cc_cands), len(cc_approved))

        self.expire_stale_entry_approvals()  # sweep any approval requests that timed out

        submitted = await self._submit(csp_approved, quote_by_occ, "csp-screener", "",
                                       review=True, account_value=account_value)
        submitted += await self._submit(cc_approved, quote_by_occ, "cc-screener", "CC")
        return submitted

    async def _submit(self, approved, quote_by_occ, rule_name: str, tag: str,
                      review: bool = False, account_value: float = 0.0) -> int:
        """Persist (dedup'd) + auto-execute a list of approved entries. Returns count submitted.

        When ``review`` and AI is enabled, each entry is analyzed just before submission: the
        verdict is stored (advisory) and, in veto mode, a 'skip' suppresses the entry. The AI never
        widens risk — it only annotates or vetoes what the screen + sizer already approved.

        Weekly premium throttle (CSP only, opt-in): once this week's collected premium reaches
        weekly_premium_target_pct of account value, further entries are PARKED for one-tap approval
        instead of auto-firing. Soft, not a hard cap.
        """
        today = utcnow().date().isoformat()
        target_pct = self.settings.entry.weekly_premium_target_pct if review else 0.0
        target_dollars = account_value * target_pct if target_pct > 0 else 0.0
        collected = (self.entry_decisions.premium_collected_since(self._week_start_iso())
                     if target_dollars > 0 else 0.0)
        reviewed = 0
        n = 0
        for entry in approved:
            c = entry.candidate
            # Resolve the broker option id for live execution (paper returns None and is fine).
            option_id = c.option_id or await self.broker.resolve_option_id(c.occ_symbol)
            key_parts = [c.underlying, c.expiration.isoformat(), str(c.strike)]
            if tag:
                key_parts.append(tag)
            key_parts.append(today)
            decision = EntryDecision(
                underlying=c.underlying,
                occ_symbol=c.occ_symbol,
                option_id=option_id,
                strike=c.strike,
                expiration=c.expiration,
                contracts=entry.contracts,
                premium=c.premium,
                rule_name=rule_name,
                reason=(f"{tag or 'CSP'} Δ={c.delta:.2f} dte={c.dte} "
                        f"ann={c.annualized_ror:.0f}% {entry.contracts}x @ ~{c.premium:.2f}"),
                dedup_key=":".join(key_parts),
            )
            if not self.entry_decisions.insert_if_new(decision):
                continue  # already decided this contract today
            self.audit.record(
                AuditEventType.DECISION,
                {"open": True, "kind": rule_name, "occ": decision.occ_symbol,
                 "contracts": decision.contracts, "reason": decision.reason},
                source="scanner", decision_id=decision.id,
            )
            quote = quote_by_occ.get(c.occ_symbol)
            if quote is None:
                self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
                continue

            # AI trade review (advisory, entries only) — annotate, and in veto mode suppress a skip.
            if (review and self.ai_reviewer is not None and self.settings.ai.enabled
                    and reviewed < self.settings.ai.max_candidates_per_scan):
                reviewed += 1
                if await self._ai_veto(decision, c):
                    continue  # veto mode + 'skip' -> do not execute

            # Weekly premium throttle: over target -> hold for approval instead of auto-firing.
            if target_dollars > 0 and collected >= target_dollars:
                await self._park_for_approval(decision, c, collected, target_dollars)
                continue

            order = await self.executor.execute_open(decision, quote)
            n += 1
            # Journal real fills only (the labeled-dataset row; observe-only).
            if order is not None and order.status == OrderStatus.FILLED:
                collected += c.premium * decision.contracts * 100  # count premium just collected
                if self.trade_journal is not None:
                    await self._journal_fill(decision, c, quote, tag)
        return n

    def _week_start_iso(self) -> str:
        """ISO timestamp for the start of the current week (Monday 00:00 UTC) — the throttle window."""
        now = utcnow()
        monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return monday.isoformat()

    async def _park_for_approval(self, decision, candidate, collected: float,
                                 target: float) -> None:
        """Hold an over-budget entry as AWAITING_APPROVAL and push a one-tap approve/reject alert."""
        self.entry_decisions.set_status(decision.id, DecisionStatus.AWAITING_APPROVAL)
        base = self.settings.public_base_url
        # Per-decision token so the link authorizes only THIS trade and can't be replayed from a
        # leaked decision id (executing a real order must not be unauthenticated).
        from ..config import entry_action_token
        tok = entry_action_token(decision.id) or ""
        q = f"?t={tok}"
        actions = [
            {"action": "http", "label": "Approve",
             "url": f"{base}/control/approve-entry/{decision.id}{q}", "method": "POST"},
            {"action": "http", "label": "Reject",
             "url": f"{base}/control/reject-entry/{decision.id}{q}", "method": "POST"},
        ]
        est = candidate.premium * decision.contracts * 100
        mins = self.settings.approval_timeout_seconds // 60
        title = f"Approve entry? {candidate.underlying} {candidate.strike:g}P"
        msg = (f"Weekly premium target reached (${collected:.0f} / ${target:.0f}) — this one's held "
               f"for your OK.\nSell {decision.contracts}x {decision.occ_symbol} for ~${est:.0f} "
               f"(Δ={candidate.delta:.2f}, {candidate.dte}d, {candidate.annualized_ror:.0f}% ann).\n"
               f"Approve within {mins} min (mode={self.settings.mode}).")
        notifier = getattr(self.executor, "notifier", None)
        if notifier is not None:
            await notifier.send(title, msg, priority="high", actions=actions)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "awaiting_approval": True,
             "reason": "weekly premium target reached", "occ": decision.occ_symbol},
            source="scanner", decision_id=decision.id,
        )

    async def _fresh_quote(self, underlying: str, occ: str):
        try:
            chain = await self.market_data.get_chain(underlying)
            return next((c for c in chain if c.occ_symbol == occ), None)
        except Exception:  # noqa: BLE001 — caller treats None as "no quote"
            return None

    async def approve_parked_entry(self, decision_id: str) -> dict:
        """One-tap approve for a throttled entry: refetch a fresh quote and execute it."""
        d = self.entry_decisions.get(decision_id)
        if d is None:
            return {"ok": False, "status": "not_found"}
        if d.status == DecisionStatus.DONE:
            return {"ok": True, "status": "already_done"}
        if d.status != DecisionStatus.AWAITING_APPROVAL:
            return {"ok": False, "status": "not_pending", "detail": d.status.value}
        if utcnow() > d.created_at + timedelta(seconds=self.settings.approval_timeout_seconds):
            self.entry_decisions.set_status(decision_id, DecisionStatus.EXPIRED)
            return {"ok": False, "status": "expired"}
        self.entry_decisions.set_status(decision_id, DecisionStatus.APPROVED)
        quote = await self._fresh_quote(d.underlying, d.occ_symbol)
        if quote is None:
            self.entry_decisions.set_status(decision_id, DecisionStatus.FAILED)
            return {"ok": False, "status": "no_quote"}
        order = await self.executor.execute_open(d, quote)
        ok = order is not None and order.status == OrderStatus.FILLED
        return {"ok": ok, "status": "executed" if ok else "not_filled"}

    async def reject_parked_entry(self, decision_id: str) -> dict:
        d = self.entry_decisions.get(decision_id)
        if d is None:
            return {"ok": False, "status": "not_found"}
        if d.status != DecisionStatus.AWAITING_APPROVAL:
            return {"ok": False, "status": "not_pending", "detail": d.status.value}
        self.entry_decisions.set_status(decision_id, DecisionStatus.REJECTED)
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "executed": False, "rejected": True, "occ": d.occ_symbol},
            source="scanner", decision_id=decision_id,
        )
        return {"ok": True, "status": "rejected"}

    def expire_stale_entry_approvals(self) -> int:
        """Mark any AWAITING_APPROVAL entry past its window EXPIRED. Returns how many."""
        now = utcnow()
        window = timedelta(seconds=self.settings.approval_timeout_seconds)
        n = 0
        for d in self.entry_decisions.list_by_status(DecisionStatus.AWAITING_APPROVAL):
            if now > d.created_at + window:
                self.entry_decisions.set_status(d.id, DecisionStatus.EXPIRED)
                n += 1
        return n

    async def _refresh_news(self, watchlist: list[str]) -> None:
        """Pull recent headlines for the whole watchlist once per scan and store them (idempotent).
        Fail-open: no provider/store or any error is swallowed so news never breaks a scan."""
        if self.news_provider is None or self.news is None:
            return
        try:
            items = await self.news_provider.fetch(
                list(watchlist), limit=50)
            if items:
                self.news.add_many(items)
        except Exception as exc:  # noqa: BLE001 — advisory; never break the scan
            log.warning("News refresh failed: %s", exc)

    def _recent_news(self, underlying: str) -> list[dict]:
        """Recent headlines for a name within the configured freshness window (advisory context)."""
        if self.news is None:
            return []
        cfg = self.settings.news
        return self.news.recent_for(
            underlying, max_age_seconds=cfg.max_age_hours * 3600,
            limit=cfg.max_items_per_symbol)

    def _enrich_ctx_from_tv(self, underlying: str, ctx: UnderlyingContext) -> None:
        """Overlay TradingView-sourced features (ADX, Bollinger %B) onto the built context so they
        are logged with every decision and available to the underlying gates. Fail-open: no store /
        no fresh snapshot / non-numeric value leaves the field None (the gates then simply skip)."""
        if self.tv_indicators is None:
            return
        snap = self.tv_indicators.get_latest(
            underlying, self.settings.ai.tv_indicator_max_age_seconds)
        payload = snap.get("payload", {}) if snap else {}
        for field in ("adx", "bb_percent_b"):
            v = payload.get(field)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                setattr(ctx, field, float(v))

    def _support_ceiling(self, underlying: str, crit) -> float | None:
        """Strike ceiling from the latest TradingView support level, for the strike-below-support
        gate. Returns None (no gate) when the gate is off or no fresh/valid support exists — the
        gate fails open so a missing alert never freezes entries. Applies support_buffer_pct as a
        margin *below* support when set.
        """
        if not getattr(crit, "require_strike_below_support", False):
            return None
        if self.tv_indicators is None:
            return None
        snap = self.tv_indicators.get_latest(
            underlying, self.settings.ai.tv_indicator_max_age_seconds)
        support = snap.get("payload", {}).get("support") if snap else None
        if not isinstance(support, (int, float)) or isinstance(support, bool):
            return None
        if not math.isfinite(support) or support <= 0:
            return None
        return support * (1.0 - max(0.0, getattr(crit, "support_buffer_pct", 0.0) or 0.0))

    async def _ai_veto(self, decision, candidate) -> bool:
        """Run the AI review for one candidate, store the verdict (advisory), and return True only
        when veto mode is on AND the model says 'skip'. Fail-open: a None verdict never suppresses."""
        ctx = self.last_context.get(candidate.underlying)
        regime = self.last_regime
        stock_dd = ctx.drawdown_20d if ctx is not None else None
        move_class = (classify_move(stock_dd, regime, self.settings.macro)
                      if regime is not None else "unknown")
        tv = None
        if self.tv_indicators is not None:
            tv = self.tv_indicators.get_latest(
                candidate.underlying, self.settings.ai.tv_indicator_max_age_seconds)
        portfolio = {"held_names": [h.symbol for h in self.last_holdings]}
        news = self._recent_news(candidate.underlying)

        verdict = await self.ai_reviewer.review(
            candidate=candidate, ctx=ctx, regime=regime, move_class=move_class,
            tv=tv, portfolio=portfolio, news=news,
        )
        if verdict is None:
            return False  # fail-open — trade proceeds under the rules

        if self.ai_reviews is not None:
            self.ai_reviews.insert(
                occ_symbol=candidate.occ_symbol, underlying=candidate.underlying,
                verdict=verdict, decision_id=decision.id,
                regime_label=(regime.label if regime is not None else None),
                move_class=move_class, model=self.settings.ai.model,
            )
        self.audit.record(
            AuditEventType.DECISION,
            {"open": True, "ai_review": verdict.as_dict(), "occ": candidate.occ_symbol,
             "move_class": move_class},
            source="scanner", decision_id=decision.id,
        )
        if self.settings.ai.mode == "veto" and verdict.recommendation == "skip":
            self.entry_decisions.set_status(decision.id, DecisionStatus.FAILED)
            log.info("AI veto: skipping %s — %s", candidate.occ_symbol, verdict.rationale[:80])
            return True
        return False

    def _record_candidates(self, csp_approved, csp_rejected, cc_approved, cc_rejected) -> None:
        """Persist one scan's full candidate disposition (approved + reasoned rejections)."""
        if self.entry_candidates is None:
            return
        import uuid
        scan_id = uuid.uuid4().hex
        rows: list[dict] = []

        def row(c, kind: str, approved: bool, reason: str, contracts=None) -> dict:
            return {
                "underlying": c.underlying, "occ_symbol": c.occ_symbol, "kind": kind,
                "strike": c.strike, "expiration": c.expiration.isoformat(), "dte": c.dte,
                "delta": c.delta, "iv": c.iv, "premium": c.premium,
                "annualized_ror": c.annualized_ror, "open_interest": c.open_interest,
                "volume": c.volume, "score": c.score, "approved": approved,
                "contracts": contracts, "reason": reason,
            }

        for e in csp_approved:
            rows.append(row(e.candidate, "CSP", True, "approved", e.contracts))
        for c, reason in csp_rejected:
            rows.append(row(c, "CSP", False, reason))
        for e in cc_approved:
            rows.append(row(e.candidate, "CC", True, "approved", e.contracts))
        for c, reason in cc_rejected:
            rows.append(row(c, "CC", False, reason))

        try:
            self.entry_candidates.record_scan(scan_id, rows, scanned_at=self.last_scan_at)
        except Exception as exc:  # noqa: BLE001 — candidate logging must never break the scan
            log.warning("entry-candidate logging failed: %s", exc)

    async def _journal_fill(self, decision: EntryDecision, candidate, quote, tag: str) -> None:
        ctx = self.last_context.get(candidate.underlying)
        context = {k: v for k, v in ctx.as_dict().items() if k != "symbol"} if ctx else {}
        # Per-option greeks at entry (extensible context; feeds the refinement dataset).
        context["theta"] = candidate.theta
        context["gamma"] = candidate.gamma
        context["theta_efficiency"] = candidate.theta_efficiency
        # IV / realized-vol ratio at entry — the variance-risk-premium signal. >1 means implied vol
        # exceeds the stock's actual movement, i.e. you're being paid MORE than the realized risk
        # (the edge premium sellers harvest). Both inputs are already captured; persist the ratio so
        # the analytics can test whether trades sold into a rich IV/RV actually pay off.
        rv = ctx.realized_vol if ctx else None
        context["iv_rv_ratio"] = (round(candidate.iv / rv, 3)
                                  if (candidate.iv and rv and rv > 0) else None)
        # Persist the MARKET REGIME at entry (previously computed then discarded). Premium-selling is
        # highly regime-dependent, so this is the metadata most likely to reveal what conditions the
        # strategy actually works in — the substrate for finding an edge.
        reg = self.last_regime
        if reg is not None:
            context["mkt_regime"] = reg.label
            context["mkt_risk_off"] = reg.risk_off
            context["mkt_spy_vol"] = reg.spy_realized_vol
            context["mkt_spy_drawdown_20d"] = reg.spy_drawdown_20d
            context["mkt_spy_above_sma200"] = reg.spy_above_sma200
        underlying_price = ctx.price if (ctx and ctx.price is not None) else None
        if underlying_price is None:
            try:
                underlying_price = await self.market_data.get_underlying_price(candidate.underlying)
            except Exception:  # noqa: BLE001 — price is best-effort context
                underlying_price = None
        self.trade_journal.insert(TradeJournalEntry(
            occ_symbol=candidate.occ_symbol,
            underlying=candidate.underlying,
            kind="CC" if tag == "CC" else "CSP",
            contracts=decision.contracts,
            strike=candidate.strike,
            dte=candidate.dte,
            delta=candidate.delta,
            iv=candidate.iv,
            premium=candidate.premium,
            spread_pct=quote.spread_pct,
            open_interest=candidate.open_interest,
            volume=candidate.volume,
            annualized_ror=candidate.annualized_ror,
            underlying_price=underlying_price,
            context=context,
            entry_decision_id=decision.id,
        ))

    @staticmethod
    def _atm_iv(chain: list[OptionContractQuote]) -> float | None:
        """IV of the contract nearest 0.50 |delta| (≈ at-the-money) — the IV-Rank seed."""
        best_iv: float | None = None
        best_dist: float | None = None
        for c in chain:
            if c.iv is None or c.delta is None:
                continue
            dist = abs(abs(c.delta) - 0.5)
            if best_dist is None or dist < best_dist:
                best_dist, best_iv = dist, c.iv
        return best_iv

    async def _context_for(self, symbol, chain, criteria) -> UnderlyingContext:
        """Build the technicals + IV-Rank context for one underlying (best-effort)."""
        try:
            # ~400 calendar days ≈ 285 trading bars — enough to compute SMA200 (+ warm-up). The
            # old default (260 cal ≈ 186 trading) fell short, leaving sma200/above_sma200 null and
            # the trend gate dark. Names younger than ~200 sessions still yield None (fail-open).
            bars = await self.market_data.get_underlying_bars(symbol, 400)
        except Exception:  # noqa: BLE001 — technicals are best-effort; never break the scan
            bars = []
        iv_hist = (
            [iv for _d, iv in self.trade_journal.iv_history(symbol)]
            if self.trade_journal is not None else []
        )
        return build_context(symbol, bars, self._atm_iv(chain), iv_hist, criteria)

    def stop(self) -> None:
        self._stop.set()
