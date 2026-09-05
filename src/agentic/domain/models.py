"""Core domain dataclasses.

Plain dataclasses (not pydantic) keep the domain layer dependency-light and easy to
construct in tests. Persistence mapping lives in the ``store`` package.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

from .enums import (
    AuditEventType,
    DecisionStatus,
    Direction,
    OptionType,
    OrderStatus,
    PositionStatus,
    RuleType,
    SignalStatus,
    Strategy,
)


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Position:
    """A short options position we may close. Broker is the source of truth."""

    occ_symbol: str                      # e.g. AAPL250920C00190000
    underlying: str
    option_type: OptionType
    strategy: Strategy
    quantity: int                        # contracts (positive integer)
    strike: float
    expiration: date
    credit_received: float               # premium collected at open, PER CONTRACT
    direction: Direction = Direction.SHORT
    open_avg_price: float | None = None
    current_bid: float | None = None
    current_ask: float | None = None
    current_mark: float | None = None
    delta: float | None = None
    iv: float | None = None
    peak_profit_pct: float = 0.0         # MFE: high-water mark of profit captured (trailing exits)
    trough_profit_pct: float = 0.0       # MAE: low-water mark of profit captured (worst drawdown)
    is_paper: bool | None = None         # True paper / False real / None unknown (stamped at sync)
    status: PositionStatus = PositionStatus.OPEN
    broker_position_id: str | None = None
    option_id: str | None = None         # broker option-instrument UUID (RH MCP order leg key)
    id: str = field(default_factory=_uuid)
    opened_at: datetime | None = None
    last_synced_at: datetime | None = None

    def dte(self, today: date | None = None) -> int:
        today = today or utcnow().date()
        return (self.expiration - today).days

    @property
    def current_value(self) -> float | None:
        """Conservative cost-to-close per contract (ask if known, else mark)."""
        if self.current_ask is not None:
            return self.current_ask
        return self.current_mark


@dataclass
class EquityHolding:
    """A long share position (broker-reported). Drives covered-call selling + assignment
    detection. ``average_cost`` is the per-share cost basis used as the CC strike floor."""

    symbol: str
    quantity: int                        # shares held
    average_cost: float

    @property
    def coverable_contracts(self) -> int:
        return self.quantity // 100


@dataclass
class CloseDecision:
    """A decision that a position should be closed, produced by a rule or signal."""

    position_id: str
    rule_name: str
    rule_type: RuleType
    reason: str
    requires_approval: bool
    dedup_key: str
    status: DecisionStatus = DecisionStatus.PROPOSED
    # A notify-only decision (e.g. a DTE "action=alert" heads-up) is NEVER routed to the executor,
    # even when requires_approval is false — it only notifies. Runtime routing flag; not persisted
    # (dedup_key handles de-dup, and routing is decided at evaluate time, never from a DB read).
    notify_only: bool = False
    id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=utcnow)
    decided_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass
class Order:
    """A buy-to-close order. ``client_order_id`` is the idempotency key."""

    decision_id: str
    position_id: str
    occ_symbol: str
    quantity: int
    limit_price: float
    is_paper: bool
    client_order_id: str = field(default_factory=_uuid)
    broker_order_id: str | None = None
    option_id: str | None = None         # broker option-instrument UUID (RH MCP order leg key)
    side: str = "BUY_TO_CLOSE"
    order_type: str = "LIMIT"
    filled_qty: int = 0
    avg_fill_price: float | None = None
    status: OrderStatus = OrderStatus.PENDING
    id: str = field(default_factory=_uuid)
    submitted_at: datetime | None = None
    last_status_at: datetime | None = None


@dataclass
class EntryDecision:
    """A decision to OPEN a cash-secured put, produced by the screener + risk sizer.

    The opening-side counterpart to CloseDecision. ``dedup_key`` (underlying:exp:strike:day)
    has a UNIQUE index so the scanner doesn't re-enter the same contract repeatedly.
    """

    underlying: str
    occ_symbol: str
    option_id: str | None
    strike: float
    expiration: date
    contracts: int
    premium: float                       # per-share credit expected at open
    rule_name: str
    reason: str
    dedup_key: str
    status: DecisionStatus = DecisionStatus.PROPOSED
    id: str = field(default_factory=_uuid)
    created_at: datetime = field(default_factory=utcnow)
    decided_at: datetime | None = None


@dataclass
class TradeJournalEntry:
    """One labeled trade for the learning loop: entry-feature snapshot + backfilled outcome.

    Written at fill (outcome NULL/'open'); the outcome fields are filled when the position
    resolves (closed/expired/assigned). ``context`` is an open JSON dict for future signals
    (iv_rank, earnings_days, rsi, vix, sentiment…) so adding feeds needs no schema migration.
    Observe-only — never feeds back into trading decisions in this phase.
    """

    occ_symbol: str
    underlying: str
    kind: str                            # "CSP" | "CC"
    contracts: int
    # entry-feature snapshot
    strike: float
    dte: int
    delta: float | None = None
    iv: float | None = None
    premium: float | None = None
    spread_pct: float | None = None
    open_interest: int | None = None
    volume: int | None = None
    annualized_ror: float | None = None
    underlying_price: float | None = None
    context: dict[str, Any] = field(default_factory=dict)
    entry_decision_id: str | None = None
    # outcome (backfilled at resolution)
    status: str = "open"                 # open | win | loss | expired | assigned | called_away
    realized_pnl: float | None = None
    close_price: float | None = None
    days_held: int | None = None
    exit_reason: str | None = None
    entered_at: datetime = field(default_factory=utcnow)
    closed_at: datetime | None = None
    id: str = field(default_factory=_uuid)


@dataclass
class Signal:
    """An inbound TradingView webhook alert (Phase 3)."""

    raw: dict[str, Any]
    dedup_key: str
    token_ok: bool = False
    action: str = "close"
    match_field: str | None = None      # "occ" | "underlying"
    match_value: str | None = None
    status: SignalStatus = SignalStatus.NEW
    id: str = field(default_factory=_uuid)
    received_at: datetime = field(default_factory=utcnow)
    ttl_expires_at: datetime | None = None


@dataclass
class AuditEvent:
    """Append-only audit record."""

    event_type: AuditEventType
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "system"
    position_id: str | None = None
    decision_id: str | None = None
    order_id: str | None = None
    ts: datetime = field(default_factory=utcnow)
