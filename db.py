"""SQLite schema and connection helpers.

Every money movement is traceable: affiliates -> clicks -> registrations ->
FTDs -> wagers -> earnings -> payout_requests -> payouts (with TXIDs).
Fraud flags and dead-lettered postbacks are stored alongside, so an audit
can reconstruct any affiliate's balance from source events.
"""
import os
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS affiliates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    btc_address TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',   -- active | paused
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clicks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    click_id TEXT UNIQUE NOT NULL,
    affiliate_code TEXT NOT NULL,
    operator TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT,
    landing_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (affiliate_code) REFERENCES affiliates (code)
);

CREATE TABLE IF NOT EXISTS registrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    affiliate_code TEXT NOT NULL,
    operator TEXT NOT NULL,
    ip TEXT,
    email TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    fraud_flag INTEGER NOT NULL DEFAULT 0,
    fraud_reason TEXT,
    UNIQUE (player_id, operator),
    FOREIGN KEY (affiliate_code) REFERENCES affiliates (code)
);

CREATE TABLE IF NOT EXISTS ftds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    affiliate_code TEXT NOT NULL,
    operator TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    fraud_flag INTEGER NOT NULL DEFAULT 0,
    fraud_reason TEXT,
    FOREIGN KEY (affiliate_code) REFERENCES affiliates (code)
);

CREATE TABLE IF NOT EXISTS wagers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id TEXT NOT NULL,
    operator TEXT NOT NULL,
    wagered REAL NOT NULL DEFAULT 0,
    won REAL NOT NULL DEFAULT 0,
    bonus REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS earnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    affiliate_code TEXT NOT NULL,
    period TEXT NOT NULL,              -- YYYY-MM
    type TEXT NOT NULL,                -- CPA | NGR
    amount REAL NOT NULL,              -- USD
    basis TEXT,                        -- JSON detail of how it was computed
    tier_applied TEXT,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | paid | held
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (affiliate_code) REFERENCES affiliates (code)
);

CREATE TABLE IF NOT EXISTS payout_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    affiliate_code TEXT NOT NULL,
    period TEXT NOT NULL,              -- ISO week label, e.g. 2026-W46
    amount_usd REAL NOT NULL,
    amount_btc REAL NOT NULL,
    btc_address TEXT NOT NULL,
    status TEXT NOT NULL,              -- approved | submitted | invalid_address | below_minimum
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (affiliate_code) REFERENCES affiliates (code)
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payout_request_id INTEGER NOT NULL,
    affiliate_code TEXT NOT NULL,
    amount_btc REAL NOT NULL,
    btc_address TEXT NOT NULL,
    txid TEXT,
    status TEXT NOT NULL,              -- submitted | confirmed | failed
    submitted_at TEXT,
    confirmed_at TEXT,
    FOREIGN KEY (payout_request_id) REFERENCES payout_requests (id)
);

CREATE TABLE IF NOT EXISTS fraud_flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,         -- registration | ftd | player
    entity_id TEXT NOT NULL,
    reason TEXT NOT NULL,              -- duplicate_ip | self_referral | zero_wagering_bonus_churn | low_wagering_churn
    details TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dead_letter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    reason TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS idempotency (
    idempotency_key TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reg_ip ON registrations (ip, created_at);
CREATE INDEX IF NOT EXISTS idx_ftd_player ON ftds (player_id);
CREATE INDEX IF NOT EXISTS idx_wager_player ON wagers (player_id, created_at);
CREATE INDEX IF NOT EXISTS idx_earn_aff_period ON earnings (affiliate_code, period);
"""


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys on and Row access."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    """Create the schema if it does not exist and return a connection."""
    conn = get_conn(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
