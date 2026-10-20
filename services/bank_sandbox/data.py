"""Bank sandbox ledger (its own DB file: bank_sandbox.db). Simulated accounts, not a real bank.

Amounts are integer minor units (kuruş) to keep balance math exact. Seed balances are example values.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from services.common.db import DATA_DIR, open_db

DB_PATH = DATA_DIR / "bank_sandbox.db"

CURRENCY = "TRY"
ALPHA_ACCOUNT = "SBX-TR-ALPHA-0001"
BETA_ACCOUNT = "SBX-TR-BETA-0001"

INITIATED, ACCEPTED, SETTLED, FAILED = "INITIATED", "ACCEPTED", "SETTLED", "FAILED"
FINAL_STATUSES = (SETTLED, FAILED)

SCHEMA = """
CREATE TABLE accounts (id TEXT PRIMARY KEY, holder TEXT, currency TEXT, balance_minor INTEGER);
CREATE TABLE payments (
    payment_id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL,
    debtor_account TEXT, creditor_account TEXT, amount_minor INTEGER, currency TEXT, reference TEXT,
    status TEXT CHECK (status IN ('INITIATED', 'ACCEPTED', 'SETTLED', 'FAILED')),
    failure_reason TEXT, created_at TEXT, updated_at TEXT
);
CREATE TABLE status_history (payment_id TEXT, status TEXT, ts TEXT);
"""


def _seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT INTO accounts VALUES (?,?,?,?)",
        [(ALPHA_ACCOUNT, "Alpha Foods", CURRENCY, 20_000_00), (BETA_ACCOUNT, "Beta Markets", CURRENCY, 450_000_00)],
    )


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    return open_db(path, SCHEMA, _seed)


def accounts(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM accounts ORDER BY id")]


def account(conn: sqlite3.Connection, account_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return dict(row) if row else None


def payment(conn: sqlite3.Connection, payment_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM payments WHERE payment_id = ?", (payment_id,)).fetchone()
    if not row:
        return None
    p = dict(row)
    p["history"] = [dict(h) for h in conn.execute(
        "SELECT status, ts FROM status_history WHERE payment_id = ? ORDER BY rowid", (payment_id,))]
    return p


def payment_by_key(conn: sqlite3.Connection, idempotency_key: str) -> dict | None:
    row = conn.execute("SELECT payment_id FROM payments WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
    return payment(conn, row["payment_id"]) if row else None


def payments(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM payments ORDER BY created_at")]
