"""Coordinator API: invoice intake, negotiation, human approvals (T3), bank execution (T4), bank webhooks, audit.

Run: uvicorn services.coordinator.app:create_app --factory --port 8200
"""

from __future__ import annotations

import os
import threading
from functools import wraps
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from services.common import audit as audit_mod
from services.common import webhook_sig
from services.common.audit import AuditLog
from services.common.config import get_settings
from services.coordinator import payments
from services.coordinator.bank_client import BankClient
from services.coordinator.transactions import Coordinator, CoordinatorError
from services.ledger.client import LedgerClient, get_ledger_client


class SignRequest(BaseModel):
    role: str


class ApproveRequest(BaseModel):
    role: str
    signature: str


class AmendRequest(BaseModel):
    face_amount_x: float | None = Field(default=None, gt=0)
    discount: float | None = Field(default=None, gt=0, lt=1)


class IntakeRequest(BaseModel):
    invoice_number: str
    buyer_confirmation_sig: str | None = None


def build_coordinator(db_path: Path | None = None, ledger: LedgerClient | None = None, bank: BankClient | None = None,
                      supplier: httpx.Client | None = None, buyer: httpx.Client | None = None,
                      audit: AuditLog | None = None, settings=None) -> Coordinator:
    s = settings or get_settings()
    db_path = db_path or Path(os.environ.get("COORDINATOR_DB", payments.DB_PATH))
    # Two connections to the same DB: the webhook receiver never waits for a coordinator operation.
    tracker = payments.PaymentTracker(payments.connect(db_path), s.bank_webhook_secret)
    return Coordinator(
        payments.connect(db_path), ledger or get_ledger_client(s), bank or BankClient(s.bank_url), tracker,
        supplier or httpx.Client(base_url=s.supplier_url, timeout=60),
        buyer or httpx.Client(base_url=s.buyer_url, timeout=60), audit or AuditLog(), s,
    )


def create_app(coordinator: Coordinator | None = None) -> FastAPI:
    coord = coordinator or build_coordinator()
    tracker = coord.tracker
    lock = threading.RLock()  # the coordinator's SQLite connection: serialize every coordinator operation
    webhook_lock = threading.Lock()

    app = FastAPI(title="AuditFlow coordinator")
    app.state.coordinator, app.state.tracker = coord, tracker

    @app.exception_handler(CoordinatorError)
    def coordinator_error(_: Request, e: CoordinatorError):
        return JSONResponse({"detail": str(e), "details": e.details}, status_code=e.http_status)

    def locked(fn):
        @wraps(fn)
        def inner(*args, **kwargs):
            with lock:
                return fn(*args, **kwargs)

        return inner

    @app.get("/health")
    def health():
        return {"ok": True, "ledger_target": coord.ledger.target, "ledger": coord.ledger.addresses()}

    @app.post("/invoices/intake")
    @locked
    def intake(req: IntakeRequest):
        return coord.intake_invoice(req.invoice_number, req.buyer_confirmation_sig)

    @app.get("/deals")
    @locked
    def deals():
        return coord.deals()

    @app.get("/deals/{deal_id}")
    @locked
    def deal(deal_id: str):
        return coord.view(deal_id)

    @app.post("/deals/{deal_id}/negotiate")
    @locked
    def negotiate(deal_id: str):
        return coord.negotiate(deal_id)

    @app.get("/deals/{deal_id}/typed-data")
    @locked
    def typed_data(deal_id: str):
        """The exact EIP-712 payload a human signs (for external wallets)."""
        td = coord.typed_agreement(deal_id)
        return {**td, "message": {k: ("0x" + v.hex()) if isinstance(v, bytes) else v for k, v in td["message"].items()}}

    @app.post("/deals/{deal_id}/sign")
    @locked
    def sign(deal_id: str, req: SignRequest):
        return coord.sign_with_demo_key(deal_id, req.role)

    @app.post("/deals/{deal_id}/approve")
    @locked
    def approve(deal_id: str, req: ApproveRequest):
        return coord.approve(deal_id, req.role, req.signature)

    @app.post("/deals/{deal_id}/amend")
    @locked
    def amend(deal_id: str, req: AmendRequest):
        return coord.amend(deal_id, req.face_amount_x, req.discount, actor="ui")

    @app.post("/deals/{deal_id}/submit")
    @locked
    def submit(deal_id: str):
        return coord.submit(deal_id)

    @app.post("/deals/{deal_id}/refresh-payment")
    @locked
    def refresh(deal_id: str):
        return coord.refresh_payment(deal_id)

    @app.post("/deals/{deal_id}/cancel")
    @locked
    def cancel(deal_id: str, req: SignRequest):
        return coord.cancel(deal_id, f"{req.role}_human")

    @app.post("/webhooks/bank")
    async def bank_webhook(request: Request):
        raw = await request.body()  # verify the exact bytes that were signed

        def handle():
            with webhook_lock:  # the tracker's own connection; independent of the coordinator lock
                return tracker.handle_webhook(raw, request.headers.get(webhook_sig.HEADER))

        try:
            p = await run_in_threadpool(handle)
        except payments.WebhookRejected as e:
            coord.audit.append("bank_webhook", "webhook_rejected", "T4", {"raw_sha256": audit_mod.sha256_hex(
                raw.decode(errors="replace"))}, {"reason": e.reason})
            return JSONResponse({"accepted": False, "reason": e.reason}, status_code=e.http_status)
        # Advance the deal (e.g. SUBMITTED -> SETTLED) without making the bank wait for it.
        threading.Thread(target=locked(coord.on_payment_status), args=(p,), daemon=True).start()
        return {"accepted": True, "payment_id": p["payment_id"], "status": p["status"]}

    @app.get("/bank/payments/{payment_id}")
    @locked
    def tracked_payment(payment_id: str):
        p = tracker.get(payment_id)
        return p or JSONResponse({"detail": "unknown payment"}, status_code=404)

    @app.get("/bank/webhook-events")
    @locked
    def webhook_events():
        return tracker.events()

    @app.get("/audit/verify")
    def audit_verify():
        ok, n, msg = audit_mod.verify(coord.audit.path)
        return {"ok": ok, "entries": n, "message": msg}

    @app.get("/audit/entries")
    def audit_entries(limit: int = 200):
        return coord.audit.entries()[-limit:]

    return app
