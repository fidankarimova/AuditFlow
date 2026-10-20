"""Beta Markets' confidential policy check (runs inside the buyer workspace).

evaluate(x, d) answers only {feasible, gain_bucket}; the buyer's cash, reserve, opportunity cost and
limits never leave this workspace. This is trusted server-side code, not a zero-knowledge proof.
Calls are rate limited by the workspace API (services/common/ratelimit.py).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from engine.stress import simulate
from services.buyer import data

GAIN_BUCKET = 100.0  # TL; documented leakage trade-off


def buyer_gain(x: float, d: float, r_opp: float, days: int) -> float:
    """U_B - U_B0 = x*d - x(1-d) * r_opp * D/365."""
    return x * d - x * (1 - d) * r_opp * days / 365


def check(conn: sqlite3.Connection, invoice_number: str, x: float, d: float) -> dict:
    """Own-limit check with the names of failed limits (no values). Used by evaluate() and the Buyer Agent."""
    c = data.company(conn)
    payable = next((p for p in data.payables(conn) if p["number"] == invoice_number), None)
    if payable is None:
        return {"ok": False, "gain_bucket": 0, "failed": ["unknown_invoice"]}
    if not (0 < x <= payable["face"]) or not (0 < d < 1):
        return {"ok": False, "gain_bucket": 0, "failed": ["invoice_face_amount"]}

    as_of = date.fromisoformat(c["as_of"])
    days = (date.fromisoformat(payable["due_date"]) - as_of).days
    pay_now = x * (1 - d)
    gain = buyer_gain(x, d, c["opportunity_cost"], days)

    failed = []
    if pay_now > c["max_early_cash"]:
        failed.append("max_early_cash")
    if gain <= 0:
        failed.append("gain_vs_paying_at_due_date")
    if failed:
        return {"ok": False, "gain_bucket": 0, "failed": failed}
    profile = data.load_profile(conn)
    after = profile.with_flow(0, -pay_now).with_outflow_amount(days, payable["face"], payable["face"] - x)
    if simulate(after, "normal").p_breach > c["alpha"]:
        return {"ok": False, "gain_bucket": 0, "failed": ["reserve_risk_limit"]}
    return {"ok": True, "gain_bucket": int(round(gain / GAIN_BUCKET)), "failed": []}


def evaluate(conn: sqlite3.Connection, invoice_number: str, x: float, d: float) -> dict:
    r = check(conn, invoice_number, x, d)
    return {"feasible": r["ok"], "gain_bucket": r["gain_bucket"]}
