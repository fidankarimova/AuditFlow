"""Alpha Foods' confidential policy (runs inside the supplier workspace).

Computes the supplier's own needs and best alternative, and answers evaluate(x, d) with only
{feasible, gain_bucket}. Its cash, reserve, limits and BATNA never leave this workspace.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from functools import cached_property

from engine.alternatives import (
    BankTerms,
    ShiftOption,
    bank_loan_cost,
    best_alternative,
    price_alternatives,
)
from engine.stress import required_injection, simulate
from services.supplier import data

GAIN_BUCKET = 100.0  # TL; documented leakage trade-off


class SupplierPolicy:
    def __init__(self, conn: sqlite3.Connection, invoice_number: str):
        self.conn = conn
        self.company = data.company(conn)
        self.invoice = next((r for r in data.eligible_invoices(conn) if r["number"] == invoice_number), None)
        if self.invoice is None:
            raise ValueError(f"{invoice_number} is not an eligible invoice")
        self.profile = data.load_profile(conn)
        self.alpha = self.company["alpha"]
        as_of = date.fromisoformat(self.company["as_of"])
        self.days = (date.fromisoformat(self.invoice["due_date"]) - as_of).days
        self.bank = BankTerms(self.company["bank_rate"], self.company["bank_arrival_days"], self.company["bank_fee"])

    @cached_property
    def needed(self) -> float:
        return required_injection(self.profile, self.alpha)

    @cached_property
    def alternatives(self):
        as_of = date.fromisoformat(self.company["as_of"])
        shifts = [
            ShiftOption(r["category"], (date.fromisoformat(r["date"]) - as_of).days, r["amount"], r["shift_days"], r["shift_fee"])
            for r in data.obligations(self.conn)
            if r["shift_days"]
        ]
        return price_alternatives(self.profile, self.alpha, self.needed, self.days, self.bank, shifts)

    @cached_property
    def best(self):
        return best_alternative(self.alternatives)

    @property
    def no_action_needed(self) -> bool:
        return self.alternatives[0].feasible  # do_nothing

    @cached_property
    def batna_cost(self) -> float:
        """C_alt_S: cost of the best alternative without a deal. If none works, the expected shortfall."""
        return self.best.cost if self.best else simulate(self.profile).expected_max_shortfall

    def gain(self, x: float, d: float) -> float:
        """U_S - U_S0 = C_alt_S - (x*d + residual bank loan cost)."""
        residual = max(0.0, self.needed - x * (1 - d))
        return self.batna_cost - (x * d + bank_loan_cost(residual, self.bank, self.days))

    def check(self, x: float, d: float) -> dict:
        """Own-limit check with the names of failed limits (no values). Used by evaluate() and the Seller Agent."""
        c, face = self.company, self.invoice["face"]
        failed = []
        if self.no_action_needed:
            failed.append("no_action_needed")
        if not 0 < x <= face:
            failed.append("invoice_face_amount")
        if not 0 < d <= c["d_max"]:
            failed.append("max_discount")
        if x * (1 - d) < c["min_net"]:
            failed.append("min_net_amount")
        if failed:
            return {"ok": False, "gain_bucket": 0, "failed": failed}
        gain = self.gain(x, d)
        if gain <= 0:
            return {"ok": False, "gain_bucket": 0, "failed": ["gain_vs_best_alternative"]}
        residual = max(0.0, self.needed - x * (1 - d))
        after = self.profile.with_flow(0, x * (1 - d)).with_receivable_amount(self.invoice["number"], face - x)
        if residual > 0:
            after = after.with_flow(self.bank.arrival_days, residual)
        if simulate(after).p_breach > self.alpha:
            return {"ok": False, "gain_bucket": 0, "failed": ["liquidity_risk_limit"]}
        return {"ok": True, "gain_bucket": int(round(gain / GAIN_BUCKET)), "failed": []}

    def evaluate(self, x: float, d: float) -> dict:
        r = self.check(x, d)
        return {"feasible": r["ok"], "gain_bucket": r["gain_bucket"]}

    def summary(self) -> dict:
        """What the supplier shares with the coordinator to start a negotiation (no limits, no BATNA cost)."""
        return {
            "invoice_number": self.invoice["number"],
            "face": self.invoice["face"],
            "days_to_due": self.days,
            "no_action_needed": self.no_action_needed,
            "best_alternative": self.best.name if self.best else None,
        }
