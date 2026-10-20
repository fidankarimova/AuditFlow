"""Coordinator API. Phase 6: bank webhook receiver. Negotiation and transaction endpoints are added in Phase 7.

Run: uvicorn services.coordinator.app:create_app --factory --port 8200
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from services.common import webhook_sig
from services.common.config import get_settings
from services.coordinator import payments


def create_app(tracker: payments.PaymentTracker | None = None) -> FastAPI:
    if tracker is None:
        conn = payments.connect(Path(os.environ.get("COORDINATOR_DB", payments.DB_PATH)))
        tracker = payments.PaymentTracker(conn, get_settings().bank_webhook_secret)

    app = FastAPI(title="AuditFlow coordinator")
    app.state.tracker = tracker

    @app.post("/webhooks/bank")
    async def bank_webhook(request: Request):
        raw = await request.body()  # verify the exact bytes that were signed
        try:
            p = tracker.handle_webhook(raw, request.headers.get(webhook_sig.HEADER))
        except payments.WebhookRejected as e:
            return JSONResponse({"accepted": False, "reason": e.reason}, status_code=e.http_status)
        return {"accepted": True, "payment_id": p["payment_id"], "status": p["status"]}

    @app.get("/bank/payments/{payment_id}")
    def tracked_payment(payment_id: str):
        p = tracker.get(payment_id)
        return p or JSONResponse({"detail": "unknown payment"}, status_code=404)

    @app.get("/bank/webhook-events")
    def webhook_events():
        return tracker.events()

    return app
