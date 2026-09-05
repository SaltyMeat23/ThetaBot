"""Enumerations used across the domain."""
from __future__ import annotations

from enum import Enum


class OptionType(str, Enum):
    CALL = "CALL"
    PUT = "PUT"


class Strategy(str, Enum):
    COVERED_CALL = "COVERED_CALL"
    CASH_SECURED_PUT = "CASH_SECURED_PUT"
    OTHER = "OTHER"


class Direction(str, Enum):
    SHORT = "SHORT"  # sold to open (the only thing we close)
    LONG = "LONG"


class PositionStatus(str, Enum):
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    ASSIGNED = "ASSIGNED"
    EXPIRED = "EXPIRED"


class RuleType(str, Enum):
    PROFIT_TARGET = "PROFIT_TARGET"
    STOP_LOSS = "STOP_LOSS"
    ROLL = "ROLL"
    DTE = "DTE"
    SIGNAL = "SIGNAL"


class DecisionStatus(str, Enum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXECUTING = "EXECUTING"
    DONE = "DONE"
    FAILED = "FAILED"


class Autonomy(str, Enum):
    AUTO = "AUTO"
    APPROVAL = "APPROVAL"


class OrderStatus(str, Enum):
    PENDING = "PENDING"      # row created, not yet submitted to broker
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class SignalStatus(str, Enum):
    NEW = "NEW"
    MATCHED = "MATCHED"
    NO_MATCH = "NO_MATCH"
    CONSUMED = "CONSUMED"


class AuditEventType(str, Enum):
    POLL = "POLL"
    QUOTE = "QUOTE"
    DECISION = "DECISION"
    APPROVAL_REQ = "APPROVAL_REQ"
    APPROVAL_RESP = "APPROVAL_RESP"
    ORDER_SUBMIT = "ORDER_SUBMIT"
    ORDER_FILL = "ORDER_FILL"
    RECONCILE = "RECONCILE"
    KILLSWITCH = "KILLSWITCH"
    SIGNAL = "SIGNAL"
    ERROR = "ERROR"
