"""SQLite connection + schema management.

A single ``Database`` wraps one sqlite3 connection in WAL mode. The schema is applied
idempotently on open via ``CREATE TABLE IF NOT EXISTS``. The repositories
(``PositionStore``, ``AuditStore``, ...) take a ``Database`` and own their tables.

Concurrency note: the monitor/reconcile/web tasks all run in one asyncio process, so a
single connection with a short busy-timeout is sufficient. DB calls are quick and
synchronous; if they ever get heavy, wrap them in ``asyncio.to_thread``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    broker_position_id TEXT,
    option_id       TEXT,                   -- broker option-instrument UUID (RH order leg key)
    occ_symbol      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    option_type     TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    direction       TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,           -- ISO date
    credit_received REAL NOT NULL,
    open_avg_price  REAL,
    current_bid     REAL,
    current_ask     REAL,
    current_mark    REAL,
    delta           REAL,
    iv              REAL,
    peak_profit_pct REAL NOT NULL DEFAULT 0,   -- MFE: high-water mark of profit captured (trailing exits)
    trough_profit_pct REAL NOT NULL DEFAULT 0, -- MAE: low-water mark of profit captured (worst drawdown)
    is_paper        INTEGER,                    -- 1 paper / 0 real / NULL unknown (stamped at sync
                                                -- from the active broker; drives real-only views)
    status          TEXT NOT NULL,
    opened_at       TEXT,
    last_synced_at  TEXT
);
-- NON-unique: one row per OPEN EPISODE of a contract. Re-selling the same OCC after a close
-- inserts a NEW row so the closed trade's P&L survives (stats would otherwise lose it).
CREATE INDEX IF NOT EXISTS idx_positions_occ ON positions(occ_symbol, status);

CREATE TABLE IF NOT EXISTS decisions (
    id              TEXT PRIMARY KEY,
    position_id     TEXT NOT NULL,
    rule_name       TEXT NOT NULL,
    rule_type       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    requires_approval INTEGER NOT NULL,
    status          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT,
    expires_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_dedup ON decisions(dedup_key);

CREATE TABLE IF NOT EXISTS orders (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL,
    position_id     TEXT NOT NULL,
    client_order_id TEXT NOT NULL,
    broker_order_id TEXT,
    option_id       TEXT,                   -- broker option-instrument UUID (RH order leg key)
    occ_symbol      TEXT NOT NULL,
    side            TEXT NOT NULL,
    order_type      TEXT NOT NULL,
    quantity        INTEGER NOT NULL,
    limit_price     REAL NOT NULL,
    filled_qty      INTEGER NOT NULL DEFAULT 0,
    avg_fill_price  REAL,
    status          TEXT NOT NULL,
    is_paper        INTEGER NOT NULL,
    submitted_at    TEXT,
    last_status_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_client ON orders(client_order_id);

CREATE TABLE IF NOT EXISTS entry_decisions (
    id              TEXT PRIMARY KEY,
    underlying      TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    option_id       TEXT,
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,           -- ISO date
    contracts       INTEGER NOT NULL,
    premium         REAL NOT NULL,
    rule_name       TEXT NOT NULL,
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    decided_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_entry_decisions_dedup ON entry_decisions(dedup_key);

CREATE TABLE IF NOT EXISTS trade_journal (
    id              TEXT PRIMARY KEY,
    entry_decision_id TEXT,
    occ_symbol      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- CSP | CC
    contracts       INTEGER NOT NULL,
    strike          REAL NOT NULL,
    dte             INTEGER NOT NULL,
    delta           REAL,
    iv              REAL,
    premium         REAL,
    spread_pct      REAL,
    open_interest   INTEGER,
    volume          INTEGER,
    annualized_ror  REAL,
    underlying_price REAL,
    context         TEXT NOT NULL,           -- JSON: extensible future signals
    status          TEXT NOT NULL,           -- open | win | loss | expired | assigned | called_away
    realized_pnl    REAL,
    close_price     REAL,
    days_held       INTEGER,
    exit_reason     TEXT,
    entered_at      TEXT NOT NULL,
    closed_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_journal_occ_status ON trade_journal(occ_symbol, status);

-- Every screened entry candidate + its disposition, one row per (scan, contract). Captures the
-- NEGATIVE examples (screened but not entered) with the exact gate that rejected them, so the
-- model can be refined on why candidates lost — not just on the trades that filled.
CREATE TABLE IF NOT EXISTS entry_candidates (
    id              TEXT PRIMARY KEY,
    scan_id         TEXT NOT NULL,            -- groups all candidates from one scan cycle
    scanned_at      TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    kind            TEXT NOT NULL,            -- CSP | CC
    strike          REAL NOT NULL,
    expiration      TEXT NOT NULL,            -- ISO date
    dte             INTEGER NOT NULL,
    delta           REAL,
    iv              REAL,
    premium         REAL,
    annualized_ror  REAL,
    open_interest   INTEGER,
    volume          INTEGER,
    score           REAL,
    approved        INTEGER NOT NULL,         -- 1 = sized + submitted this scan, 0 = rejected
    contracts       INTEGER,                  -- sized size when approved
    reason          TEXT NOT NULL,            -- 'approved' or the gate that rejected it
    scanned_date    TEXT NOT NULL             -- ISO date, for cheap day-level dedup/pruning
);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_scan ON entry_candidates(scanned_at);
CREATE INDEX IF NOT EXISTS idx_entry_candidates_occ ON entry_candidates(occ_symbol, scanned_at);

CREATE TABLE IF NOT EXISTS iv_history (
    symbol      TEXT NOT NULL,
    date        TEXT NOT NULL,               -- ISO date
    atm_iv      REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS signals (
    id              TEXT PRIMARY KEY,
    raw             TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    token_ok        INTEGER NOT NULL,
    action          TEXT NOT NULL,
    match_field     TEXT,
    match_value     TEXT,
    status          TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    ttl_expires_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup ON signals(dedup_key);

-- Latest TradingView indicator snapshot per symbol (entry-context channel; NOT a close signal).
CREATE TABLE IF NOT EXISTS tv_indicators (
    symbol      TEXT PRIMARY KEY,
    raw         TEXT NOT NULL,               -- JSON: the full TradingView alert payload
    received_at TEXT NOT NULL
);

-- AI trade-analyst verdicts (advisory), one per reviewed entry candidate.
CREATE TABLE IF NOT EXISTS ai_reviews (
    id             TEXT PRIMARY KEY,
    decision_id    TEXT,
    occ_symbol     TEXT NOT NULL,
    underlying     TEXT NOT NULL,
    recommendation TEXT NOT NULL,            -- take | caution | skip
    confidence     REAL,
    rationale      TEXT,
    flags          TEXT,                     -- JSON list
    regime_label   TEXT,
    move_class     TEXT,                     -- systemic | idiosyncratic | neutral | unknown
    model          TEXT,
    verdict        TEXT NOT NULL,            -- full JSON verdict
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ai_reviews_created ON ai_reviews(created_at);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL,
    position_id TEXT,
    decision_id TEXT,
    order_id    TEXT,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

-- Recent news / catalyst items per symbol (entry-context channel; advisory, never a trade-picker).
-- Fed by the Alpaca (Benzinga) pull and a webhook push (curated X / other feeds).
CREATE TABLE IF NOT EXISTS news_items (
    dedup_key   TEXT PRIMARY KEY,           -- source:id:symbol, so re-ingest is idempotent
    symbol      TEXT NOT NULL,
    headline    TEXT NOT NULL,
    source      TEXT,                        -- benzinga | x | ...
    url         TEXT,
    created_at  TEXT,                        -- article/post timestamp (ISO), for recency
    received_at TEXT NOT NULL                -- when we stored it
);
CREATE INDEX IF NOT EXISTS idx_news_symbol_created ON news_items(symbol, created_at);

-- single-row control table for the kill switch / global pause
CREATE TABLE IF NOT EXISTS control (
    id      INTEGER PRIMARY KEY CHECK (id = 1),
    paused  INTEGER NOT NULL DEFAULT 0,
    reason  TEXT,
    updated_at TEXT
);
INSERT OR IGNORE INTO control (id, paused) VALUES (1, 0);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.migrate()

    def migrate(self) -> None:
        self.conn.executescript(SCHEMA)
        # Idempotent column adds for DBs created before option_id existed (CREATE TABLE
        # IF NOT EXISTS won't alter an existing table). ADD COLUMN raises if it's already
        # there — swallow that and move on.
        for table in ("positions", "orders"):
            try:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN option_id TEXT")
            except sqlite3.OperationalError:
                pass  # column already present
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN peak_profit_pct REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already present
        try:
            self.conn.execute(
                "ALTER TABLE positions ADD COLUMN trough_profit_pct REAL NOT NULL DEFAULT 0"
            )
        except sqlite3.OperationalError:
            pass  # column already present
        try:
            self.conn.execute("ALTER TABLE positions ADD COLUMN is_paper INTEGER")
        except sqlite3.OperationalError:
            pass  # column already present
        # Replace the old UNIQUE(occ_symbol) index (which overwrote a closed trade when the same
        # contract was re-sold) with a non-unique one, so each open episode gets its own row.
        try:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_positions_occ'"
            ).fetchone()
            if row and row["sql"] and "UNIQUE" in row["sql"].upper():
                self.conn.execute("DROP INDEX idx_positions_occ")
                self.conn.execute(
                    "CREATE INDEX idx_positions_occ ON positions(occ_symbol, status)"
                )
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
