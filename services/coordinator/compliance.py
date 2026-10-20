"""Compliance Agent (coordinator). Tools: check_invoice, check_agreement. It flags; it cannot change policies or
limits. All checks are deterministic code; any failed blocking check stops the flow in the coordinator."""

from __future__ import annotations

import re
from datetime import date

from functools import partial

from services.common.audit import AuditLog
from services.common.config import Settings
from services.common.eip712 import BPS, pay_amount
from services.common.llm import Tool, ToolRegistry
from services.coordinator import parties

MAX_DISCOUNT_BPS = 500  # public cap, same as the negotiation bound (5%)
_INJECTION = re.compile(r"\b(system|ignore (your|all|previous)|instruction|override|accept .* immediately)\b", re.I)


def _finding(check: str, ok: bool, detail: str = "", blocking: bool = True) -> dict:
    return {"check": check, "ok": ok, "blocking": blocking, "detail": detail}


def check_invoice(doc: dict, today: str | None = None) -> dict:
    today = today or date.today().isoformat()
    seller, buyer = parties.get(doc.get("seller", "")), parties.get(doc.get("buyer", ""))
    f = [
        _finding("seller is an onboarded supplier", bool(seller and seller.role == "supplier"), doc.get("seller", "")),
        _finding("buyer is an onboarded buyer", bool(buyer and buyer.role == "buyer"), doc.get("buyer", "")),
        _finding("positive face amount", float(doc.get("face", 0)) > 0),
        _finding("currency is TRY", doc.get("currency") == "TRY", str(doc.get("currency"))),
        _finding("due date in the future", str(doc.get("due_date", "")) > today, str(doc.get("due_date"))),
        _finding("invoice not disputed or settled", doc.get("status") == "open", str(doc.get("status"))),
        _finding("payee account is the seller's registered account",
                 bool(seller) and doc.get("payee_account") == seller.bank_account, str(doc.get("payee_account"))),
        _finding("memo free of instruction-like text", not _INJECTION.search(doc.get("memo") or ""),
                 "memo looks like a prompt injection; treated as untrusted data" if _INJECTION.search(
                     doc.get("memo") or "") else "", blocking=False),
    ]
    return {"ok": all(x["ok"] for x in f if x["blocking"]), "findings": f}


def check_agreement(agreement: dict, doc: dict, now_ts: int, settings: Settings | None = None) -> dict:
    seller, buyer = parties.get(doc["seller"]), parties.get(doc["buyer"])
    face_minor = round(float(doc["face"]) * 100)
    f = [
        _finding("seller address is the seller's approver", bool(seller) and agreement["seller"] == seller.signer(settings)),
        _finding("buyer address is the buyer's approver", bool(buyer) and agreement["buyer"] == buyer.signer(settings)),
        _finding("amount within invoice face", 0 < agreement["face_amount_x"] <= face_minor),
        _finding("currency matches invoice", agreement["currency"] == doc["currency"]),
        _finding("payee account matches registered account",
                 bool(seller) and agreement["payee_account"] == seller.bank_account),
        _finding("discount within public cap", 0 < agreement["discount_bps"] <= MAX_DISCOUNT_BPS,
                 f"{agreement['discount_bps']} bps"),
        _finding("pay amount = face x (1 - discount)",
                 agreement["pay_amount"] == pay_amount(agreement["face_amount_x"], agreement["discount_bps"])
                 and agreement["discount_bps"] < BPS),
        _finding("not expired", agreement["expires_at"] > now_ts),
    ]
    return {"ok": all(x["ok"] for x in f), "findings": f}


def compliance_agent(audit: AuditLog | None = None, settings: Settings | None = None) -> ToolRegistry:
    return ToolRegistry.of("compliance_agent", [
        Tool("check_invoice", "Check an invoice document for mismatches and risks.",
             {"type": "object", "properties": {"doc": {"type": "object"}}, "required": ["doc"]}, check_invoice),
        Tool("check_agreement", "Check agreement terms against the invoice.",
             {"type": "object", "properties": {"agreement": {"type": "object"}, "doc": {"type": "object"},
                                               "now_ts": {"type": "integer"}},
              "required": ["agreement", "doc", "now_ts"]}, partial(check_agreement, settings=settings)),
    ], audit)
