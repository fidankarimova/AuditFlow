"""Beta Markets (buyer) workspace data. Only this package opens beta_markets.db.

These values are private to the buyer: the coordinator and the supplier never read them.
All amounts are example values in TL.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from engine.stress import LiquidityProfile
from services.common.db import DATA_DIR, open_db

DB_PATH = DATA_DIR / "beta_markets.db"

SCHEMA = """
CREATE TABLE company (
    name TEXT, as_of TEXT, cash REAL, reserve REAL,
    sales_mean REAL, sales_sd REAL, expense_mean REAL, expense_sd REAL,
    opportunity_cost REAL, max_early_cash REAL, alpha REAL
);
CREATE TABLE payables (id INTEGER PRIMARY KEY, number TEXT UNIQUE, supplier TEXT, face REAL, due_date TEXT);
CREATE TABLE scheduled_outflows (date TEXT, amount REAL, category TEXT);
"""


def _seed(conn: sqlite3.Connection, as_of: date | None = None) -> None:
    as_of = as_of or date.today()
    d = lambda days: (as_of + timedelta(days=days)).isoformat()  # noqa: E731
    conn.execute(
        "INSERT INTO company VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("Beta Markets", as_of.isoformat(), 450_000, 300_000, 30_000, 3_000, 24_000, 2_000, 0.09, 120_000, 0.02),
    )
    conn.execute(
        "INSERT INTO payables (number, supplier, face, due_date) VALUES (?,?,?,?)",
        (f"ALF-{os.environ.get('INVOICE_SERIES', '2026')}-0142", "Alpha Foods", 100_000, d(60)),
    )
    conn.executemany(
        "INSERT INTO scheduled_outflows VALUES (?,?,?)",
        [(d(7), 30_000, "supplier batch"), (d(15), 45_000, "payroll"), (d(30), 15_000, "tax"), (d(45), 45_000, "payroll")],
    )


def connect(path: Path = DB_PATH, as_of: date | None = None) -> sqlite3.Connection:
    return open_db(path, SCHEMA, lambda c: _seed(c, as_of))


def company(conn: sqlite3.Connection) -> dict:
    return dict(conn.execute("SELECT * FROM company").fetchone())


def payables(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM payables ORDER BY due_date")]


def load_profile(conn: sqlite3.Connection) -> LiquidityProfile:
    c = company(conn)
    as_of = date.fromisoformat(c["as_of"])
    days = lambda iso: (date.fromisoformat(iso) - as_of).days  # noqa: E731
    outs = [(days(r["date"]), r["amount"]) for r in conn.execute("SELECT * FROM scheduled_outflows")]
    outs += [(days(p["due_date"]), p["face"]) for p in payables(conn)]
    return LiquidityProfile(
        name=c["name"], cash0=c["cash"], reserve=c["reserve"], scheduled_outflows=tuple(outs),
        sales_mean=c["sales_mean"], sales_sd=c["sales_sd"],
        expense_mean=c["expense_mean"], expense_sd=c["expense_sd"], as_of=as_of,
    )


def check_invoice(conn: sqlite3.Connection, doc: dict) -> dict:
    """Does a supplier's invoice match a payable Beta Markets actually owes? Used before its human confirms."""
    payable = next((p for p in payables(conn) if p["number"] == doc.get("number")), None)
    if payable is None:
        return {"matches": False, "mismatches": ["no such payable"]}
    mismatches = []
    if doc.get("seller") != payable["supplier"]:
        mismatches.append("seller")
    if doc.get("buyer") != company(conn)["name"]:
        mismatches.append("buyer")
    if abs(float(doc.get("face", -1)) - payable["face"]) > 0.005:
        mismatches.append("face amount")
    if doc.get("due_date") != payable["due_date"]:
        mismatches.append("due date")
    if doc.get("currency") != "TRY":
        mismatches.append("currency")
    return {"matches": not mismatches, "mismatches": mismatches}
