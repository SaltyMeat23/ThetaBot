"""Config + secrets loading.

Behavior/config comes from ``config.yaml`` (typed via pydantic). Secrets come from
the process environment / ``.env`` (loaded lazily, never written to config). The
live-mode arming gate lives here: live trading requires BOTH ``mode == "live"`` AND
``i_understand_live_trading == True``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
DEFAULT_EXAMPLE_PATH = REPO_ROOT / "config.example.yaml"
# Writable overlay (lives in the persistent data volume, NOT the read-only mounted base config)
# where the in-app settings editor persists runtime edits. Deep-merged over the base at load.
OVERLAY_PATH = REPO_ROOT / "data" / "config_overlay.yaml"


class ExecutionConfig(BaseModel):
    limit_buffer_pct: float = 0.02
    slippage_cap_pct: float = 0.05
    fill_timeout_seconds: int = 60
    reprice_after_seconds: int = 20


class RobinhoodConfig(BaseModel):
    # The Robinhood brokerage account that automated closes route through. MUST be an
    # agentic_allowed=true, option_level_2+ account (per get_accounts). Every RH MCP
    # options tool requires account_number — without it the broker stays read-only.
    account_number: str = ""


class EntryCriteria(BaseModel):
    # Screening thresholds for CSP candidates (research-backed defaults; tune per account).
    delta_min: float = 0.20            # short-put |delta| band (0.16-0.40 typical)
    delta_max: float = 0.30            # sweet spot 0.25-0.30
    dte_min: int = 30                  # 30-45 DTE window (avoid <14)
    dte_max: int = 45
    min_annualized_yield: float = 0.20  # (premium/strike)/dte*365 floor, e.g. 0.20 = 20%
    min_open_interest: int = 100
    min_volume: int = 10
    max_spread_pct: float = 0.15       # bid-ask spread as fraction of mid
    exclude_earnings_days: int = 7     # skip if earnings within N days (0 = ignore)
    # Underlying gates (entry intelligence) — all opt-in; None/False = don't gate.
    min_iv_rank: float | None = None       # skip name if IV Rank below this (0-100), when known
    rsi_min: float | None = None           # skip if RSI below this (avoid deep oversold falling knife)
    rsi_max: float | None = None           # skip if RSI above this
    require_above_sma200: bool = False     # only sell puts on names above their 200-day (uptrend)
    # Graded alternative to require_above_sma200: sell into a shallow dip but not a broken downtrend.
    # Skip if price is MORE than this fraction below the 200-SMA (0.10 = allow down to 10% below,
    # block deeper). None = don't gate. Fail-open when price/sma200 are unknown. A name at/above the
    # 200-SMA always passes.
    max_pct_below_sma200: float | None = None
    # TradingView-fed gates (opt-in; None = don't gate; fail-open when no fresh adx/bb%b alert).
    min_adx: float | None = None           # skip if daily ADX below this (weak/choppy trend, ~20-25 typical)
    min_bb_percent_b: float | None = None  # skip if Bollinger %B below this (price pinned to the lower band)
    iv_rank_min_history_days: int = 60     # IV-Rank stays None until this many daily IVs exist
    # Strike-below-support gate (opt-in): only sell a put whose strike sits at/below the latest
    # TradingView support level, so support cushions a decline before assignment is threatened.
    # Fails open — when no fresh support snapshot exists for the name, the gate simply doesn't apply.
    require_strike_below_support: bool = False
    support_buffer_pct: float = 0.0        # require strike this fraction BELOW support (0 = at/below)


class EntrySizing(BaseModel):
    max_position_size_pct: float = 0.10        # cap each CSP at X% of account value (research: ~10%)
    max_concurrent_positions: int = 5          # max open CSPs
    max_concurrent_cc: int = 10                # max open covered calls (covered, lower risk)
    total_bp_utilization_target: float = 0.50  # never commit more than X% of buying power total
    buying_power_reserve_pct: float = 0.10     # always keep X% of buying power unused
    # Scale-invariant sizing (opt-in). When target_positions is set, size by spreading committed
    # collateral across ~N names — the per-name budget (and %) shrinks automatically as the account
    # grows, so the SAME config works from $5k to $500k. max_position_size_pct then acts as a
    # per-name BACKSTOP ceiling (only binds at small accounts, where a single contract must exceed
    # the diversified share). None = legacy per-name-% sizing.
    target_positions: int | None = None
    # Liquidity cap (opt-in): never take more than this fraction of a strike's open interest.
    # Fail-open when OI is unknown. Protects large accounts from over-filling thin options. None = off.
    max_pct_of_oi: float | None = None


class EntryConfig(BaseModel):
    enabled: bool = False                      # master off-switch; scanner won't run unless true
    watchlist: list[str] = Field(default_factory=list)
    feed: Literal["indicative", "opra"] = "indicative"  # opra (real-time) REQUIRED for live entry
    scan_interval_seconds: int = 300
    # Skip a name whose earnings fall within (dte_max + exclude_earnings_days) days — so a short
    # put is never held through an earnings report. Enforced only when an earnings source is
    # available (RH MCP + ROBINHOOD_MCP_TOKEN); otherwise fails open (never blocks).
    earnings_gate: bool = True
    # Prefer selling premium when it's RICH: rank candidates by the underlying's IV rank (current IV
    # vs its own recent history) before theta-efficiency, so limited capital is deployed into the
    # names paying the most relative to their norm — the volatility-risk-premium edge. Fail-open:
    # names with unknown IV rank (too little history) are treated as neutral, never penalized. Off by
    # default (pure theta-efficiency ranking).
    prefer_iv_rank: bool = False
    # Soft weekly over-trading throttle: once THIS week's collected CSP premium reaches this fraction
    # of account value, further auto-entries are held for one-tap approval instead of firing
    # automatically (not a hard cap). 0 = off. e.g. 0.02 = "auto-trade until ~2%/week, then ask me".
    weekly_premium_target_pct: float = 0.0
    criteria: EntryCriteria = Field(default_factory=EntryCriteria)      # CSP screening
    cc_criteria: EntryCriteria = Field(default_factory=EntryCriteria)   # covered-call screening
    sizing: EntrySizing = Field(default_factory=EntrySizing)
    # Per-ticker criteria overrides, e.g. {"NVDA": {"delta_max": 0.25}} — merged over `criteria`
    # / `cc_criteria` so we can tune each name we know well.
    per_ticker: dict[str, dict] = Field(default_factory=dict)

    def criteria_for(self, symbol: str, base: EntryCriteria) -> EntryCriteria:
        """Apply this symbol's per-ticker overrides on top of a base criteria set."""
        overrides = self.per_ticker.get(symbol.upper()) or self.per_ticker.get(symbol) or {}
        return base.model_copy(update=overrides) if overrides else base


class RegimeConfig(BaseModel):
    # Market-regime read (SPY/QQQ index-ETF trend + drawdown + realized-vol) so the bot can tell
    # "the whole market is in free-fall" from "just a blip on this stock". Deterministic flags;
    # the AI reviewer interprets them. VIX-proper isn't reachable via Alpaca yet, so v1 uses SPY
    # 20-day realized vol as the fear proxy.
    enabled: bool = True
    symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ"])  # [market, tech] proxies
    lookback_days: int = 220               # daily bars to pull (>=200 for the 200-SMA)
    elevated_vol: float = 0.20             # SPY annualized realized vol >= -> "elevated" (VIX~20)
    risk_off_vol: float = 0.30             # >= -> "risk_off" (VIX~30)
    elevated_drawdown: float = 0.05        # SPY 20d drawdown >= 5% -> "elevated"
    risk_off_drawdown: float = 0.10        # >= 10% -> "risk_off"
    systemic_drawdown: float = 0.05        # market 20d dd >= 5% -> a stock's drop reads "systemic"
    stock_move_min: float = 0.03           # a stock must be down >= 3% to count as "a move" at all
    hard_gate: bool = False                # true = skip ALL new entries while risk_off (default off:
                                           # flags feed the AI, they don't hard-block)


class AIConfig(BaseModel):
    # AI trade-analyst (advisory). Reviews already-screened + already-sized CSP candidates just
    # before submission and records a structured verdict. HARD guardrail: it can only flag or skip,
    # NEVER widen risk, increase size, or approve anything the screen rejected. Off until an
    # ANTHROPIC_API_KEY is present.
    enabled: bool = False
    model: str = "claude-opus-4-8"
    mode: Literal["advisory", "veto"] = "advisory"    # advisory = annotate only; veto = skip on "skip"
    max_candidates_per_scan: int = 3                   # cap Claude calls per scan (cost control)
    cache_ttl_seconds: int = 3600                      # reuse a verdict for the same (occ, day)
    # Daily S/R alerts fire once per session, so a snapshot is routinely 17-24h old during market
    # hours — a 1h window would hide it from the AI entirely. 30h keeps the latest daily read valid
    # all day, and a missed day goes stale (ignored) rather than lingering forever.
    tv_indicator_max_age_seconds: int = 108000         # ignore TradingView snapshots older than this
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"


class RollConfig(BaseModel):
    # Roll a TESTED short put near expiry instead of realizing a loss or accepting an unfavorable
    # assignment: buy it back and sell a further-dated put (down a strike if needed) for a NET
    # CREDIT — lowering the effective cost basis and buying time. Only rolls for a credit; if no
    # acceptable roll exists it leaves the position alone (rides to assignment).
    enabled: bool = False
    roll_dte: int = 3                  # only consider rolling within this many days of expiry
    roll_delta: float = 0.45           # ...and only when the put is tested (|delta| >= this)
    target_dte_min: int = 7            # roll OUT to a new expiry in this window
    target_dte_max: int = 14
    min_net_credit: float = 0.0        # require net credit >= this (0 = never pay to roll)
    max_target_delta: float = 0.35     # don't roll into an even more-tested put


class ReportingConfig(BaseModel):
    # Scheduled push reports for hands-off operation: a daily digest (heartbeat: "here's what I did /
    # I'm alive") + a weekly performance rollup. Times are US/Eastern.
    enabled: bool = True
    daily_digest_hour: int = 16            # 24h ET; 16:15 = just after the close
    daily_digest_minute: int = 15
    weekly_report_weekday: int = 4         # 0=Mon .. 6=Sun; 4 = Friday
    check_interval_seconds: int = 300


class NewsConfig(BaseModel):
    # Advisory news/catalyst channel: an Alpaca (Benzinga) pull + a webhook push (curated X/other
    # feeds). Feeds the AI reviewer and a logged context feature; NEVER a trade-picker. Off by
    # default. Research: naive news-driven entry underperforms; the safe role is defense/context.
    enabled: bool = False
    provider: Literal["alpaca", "none"] = "alpaca"
    max_age_hours: int = 48                # how far back a headline stays "recent" for context
    max_items_per_symbol: int = 5          # cap headlines per name (prompt / cost control)


class NotifyConfig(BaseModel):
    provider: Literal["none", "ntfy", "pushover"] = "none"
    ntfy_topic: str = ""


class TunnelConfig(BaseModel):
    provider: Literal["none", "cloudflared", "ngrok"] = "none"
    hostname: str = ""


class RuleConfig(BaseModel):
    name: str
    rule_type: Literal["PROFIT_TARGET", "STOP_LOSS", "DTE", "SIGNAL"]
    enabled: bool = True
    requires_approval: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class WebConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    signal_ttl_seconds: int = 600  # how long a queued TradingView signal stays actionable


class RiskConfig(BaseModel):
    """Portfolio loss circuit breaker for a premium-SELLING book. Freezes NEW entries when realized
    losses pile up — it NEVER force-closes (the monitor keeps managing/closing existing positions,
    so you never dump short puts at the bottom). Keys on REALIZED P&L and losing streaks, not
    unrealized marks: a short put's mark swing is noise for a hold-to-expiry seller, and assignment
    is the wheel working, not a loss. Auto-evaluates each scan and clears itself as the loss window
    rolls off. All opt-in / tunable."""
    loss_breaker_enabled: bool = True
    lookback_days: int = 7                  # rolling window for the realized-loss sum
    max_realized_loss_pct: float = 0.10     # freeze new entries if window realized P&L <= -X% of account value (0 = off)
    max_consecutive_losses: int = 4         # freeze new entries after K straight realized losers (0 = off)


class Settings(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    i_understand_live_trading: bool = False

    broker: Literal["paper", "robinhood_mcp", "robinstocks", "alpaca"] = "paper"
    broker_fallback: Literal["paper", "robinhood_mcp", "robinstocks", "alpaca"] | None = None
    market_data: Literal["paper", "alpaca", "robinhood"] = "paper"
    # "robinhood" (this edition's default in config.example.yaml): source all option + equity data
    #   from the SAME Robinhood connection used to trade — one login, no extra data subscription.
    # "alpaca": use Alpaca instead (real-time OPRA needs a paid Alpaca plan + ALPACA_API_* keys).
    # "paper": simulated data for dry runs / tests.
    # Simulated buying power the paper broker reports — set to your real account size so paper
    # sizing mirrors reality (default keeps the old large sandbox balance).
    paper_buying_power: float = 100_000.0
    # Seed a couple of fake demo positions into the paper broker. Convenient for local dev/tests,
    # but on a real paper soak those fixtures inject phantom collateral that zeroes the entry
    # risk budget (so the scanner can never size a trade) and clutter the dashboard with fake
    # P&L. Set false on a soak so what you see reflects only real scanner activity.
    paper_seed_positions: bool = True

    poll_interval_seconds: int = 60
    poll_interval_closed_seconds: int = 300
    reconcile_interval_seconds: int = 300
    approval_timeout_seconds: int = 900
    max_quote_age_seconds: int = 10
    auto_trip_after_errors: int = 0  # auto-engage kill switch after N consecutive errors (0=off)

    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    robinhood: RobinhoodConfig = Field(default_factory=RobinhoodConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    macro: RegimeConfig = Field(default_factory=RegimeConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    roll: RollConfig = Field(default_factory=RollConfig)
    reporting: ReportingConfig = Field(default_factory=ReportingConfig)
    notify: NotifyConfig = Field(default_factory=NotifyConfig)
    tunnel: TunnelConfig = Field(default_factory=TunnelConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    rules: list[RuleConfig] = Field(default_factory=list)

    # --- derived helpers ---
    @property
    def is_live(self) -> bool:
        """True only when explicitly armed for live trading."""
        return self.mode == "live" and self.i_understand_live_trading

    @property
    def public_base_url(self) -> str:
        """Base URL for one-tap approval links. Uses the public tunnel hostname when set
        (so the buttons work from a phone), else the local control port."""
        if self.tunnel.hostname:
            host = self.tunnel.hostname
            return host if host.startswith("http") else f"https://{host}"
        return f"http://localhost:{self.web.port}"

    @property
    def db_path(self) -> Path:
        return REPO_ROOT / "data" / "agentic.db"


def _deep_merge(base: dict, patch: dict) -> dict:
    """Recursively merge ``patch`` into ``base`` (dicts merge; scalars/lists replace)."""
    out = dict(base)
    for key, val in patch.items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_overlay(path: str | Path | None = None) -> dict:
    """Read the runtime settings overlay (empty dict if none)."""
    p = Path(path) if path is not None else OVERLAY_PATH
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def save_overlay(overlay: dict, path: str | Path | None = None) -> None:
    """Persist the runtime settings overlay to the writable data volume."""
    p = Path(path) if path is not None else OVERLAY_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(overlay, sort_keys=False), encoding="utf-8")


def load_config(path: str | Path | None = None, *, apply_overlay: bool = True) -> Settings:
    """Load Settings from a YAML file, falling back to config.example.yaml.

    The example file lets the skeleton run before the user copies their own config. When
    ``apply_overlay`` is set (default), any runtime edits saved via the in-app settings editor
    (``OVERLAY_PATH``) are deep-merged over the base so they survive restarts.
    """
    candidate = Path(path) if path else DEFAULT_CONFIG_PATH
    if not candidate.exists():
        candidate = DEFAULT_EXAMPLE_PATH
    data = yaml.safe_load(candidate.read_text(encoding="utf-8")) or {}
    if apply_overlay:
        overlay = load_overlay()
        if overlay:
            data = _deep_merge(data, overlay)
    return Settings.model_validate(data)


def get_secret(name: str, default: str | None = None) -> str | None:
    """Read a secret from the environment. Loads .env on first call if present."""
    _ensure_dotenv_loaded()
    return os.environ.get(name, default)


def _action_token(kind: str, decision_id: str) -> str | None:
    """Per-decision one-tap approval credential = HMAC(CONTROL_TOKEN, "kind:decision_id").

    Lets an approval notification link authorize ONLY that specific, already-vetted action —
    without embedding the master CONTROL_TOKEN or requiring a dashboard login (so the phone one-tap
    flow is preserved). ``kind`` (entry/close) domain-separates the two so a token for one can't
    authorize the other. Returns None when no CONTROL_TOKEN is set -> the endpoints must refuse.
    """
    import hashlib
    import hmac as _hmac

    secret = get_secret("CONTROL_TOKEN")
    if not secret:
        return None
    return _hmac.new(secret.encode(), f"{kind}:{decision_id}".encode(), hashlib.sha256).hexdigest()


def entry_action_token(decision_id: str) -> str | None:
    """One-tap token for approving a throttled ENTRY (places a real order)."""
    return _action_token("entry", decision_id)


def close_action_token(decision_id: str) -> str | None:
    """One-tap token for approving a gated CLOSE (buys to close a real position)."""
    return _action_token("close", decision_id)


_dotenv_loaded = False


def _ensure_dotenv_loaded() -> None:
    """Minimal .env loader (no external dependency). Does not override real env vars."""
    global _dotenv_loaded
    if _dotenv_loaded:
        return
    _dotenv_loaded = True
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)
