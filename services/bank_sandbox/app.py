"""Bank sandbox API: a simulated bank, run as its own service with its own DB. No real money, no real bank.

- POST /payments with an `Idempotency-Key` header. Same key + same body => the same payment, no second debit.
  Same key + different body => 409.
- Progression INITIATED -> ACCEPTED (debtor debited) -> SETTLED (creditor credited), or FAILED (insufficient funds),
  a few seconds apart. Every status change is pushed to the coordinator as an HMAC-SHA256 signed webhook.
- Failure injection: BANK_FAIL_RATE (random 503s) and POST /admin/outage (503 for the next N payment calls,
  either before the payment is processed or after it, i.e. a lost response).
- Every request/response and every webhook is appended to the sandbox operation log (JSONL).

Run: uvicorn services.bank_sandbox.app:create_app --factory --port 8300
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.bank_sandbox import data
from services.common import webhook_sig
from services.common.config import REPO_ROOT, get_settings

LOG_PATH = REPO_ROOT / "evidence" / "bank_sandbox_log.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class JsonlLog:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: dict) -> None:
        line = json.dumps({"ts": _now(), **entry}, sort_keys=True)
        with self.lock, self.path.open("a") as f:
            f.write(line + "\n")


class PaymentRequest(BaseModel):
    debtor_account: str
    creditor_account: str
    amount_minor: int = Field(gt=0)
    currency: str = data.CURRENCY
    reference: str = Field(min_length=1, max_length=200)  # e.g. agreement hash


class OutageRequest(BaseModel):
    count: int = Field(ge=0, le=100)
    mode: str = Field(default="before", pattern="^(before|after)$")


def _http_webhook_sender(url: str) -> Callable[[bytes, dict], int]:
    def send(body: bytes, headers: dict) -> int:
        return httpx.post(url, content=body, headers=headers, timeout=5).status_code

    return send


def _thread_executor(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def create_app(
    db_path: Path | None = None,
    log_path: Path | None = None,
    webhook_secret: str | None = None,
    webhook_sender: Callable[[bytes, dict], int] | None = None,
    step_seconds: float | None = None,
    fail_rate: float | None = None,
    executor: Callable[[Callable[[], None]], None] = _thread_executor,
    seed: int | None = None,
) -> FastAPI:
    """Builds the sandbox. Arguments override env settings (tests inject a webhook sender and an inline executor)."""
    s = get_settings()
    secret = s.bank_webhook_secret if webhook_secret is None else webhook_secret
    if not secret:
        raise RuntimeError("BANK_WEBHOOK_SECRET is not set; the bank sandbox refuses to send unsigned webhooks")
    step = s.bank_step_seconds if step_seconds is None else step_seconds
    fail_rate = s.bank_fail_rate if fail_rate is None else fail_rate
    if webhook_sender is None and s.bank_webhook_url:
        webhook_sender = _http_webhook_sender(s.bank_webhook_url)

    conn = data.connect(db_path or Path(os.environ.get("BANK_DB", data.DB_PATH)))
    log = JsonlLog(log_path or Path(os.environ.get("BANK_LOG", LOG_PATH)))
    db_lock = threading.RLock()
    rng = random.Random(seed)
    outage = {"remaining": 0, "mode": "before"}

    app = FastAPI(title="AuditFlow bank sandbox (simulated)")
    app.state.conn, app.state.log = conn, log

    @app.middleware("http")
    async def log_io(request: Request, call_next):
        body = await request.body()
        response = await call_next(request)
        resp_body = b"".join([chunk async for chunk in response.body_iterator])
        log.write({
            "kind": "http",
            "method": request.method,
            "path": request.url.path,
            "query": str(request.url.query),
            "idempotency_key": request.headers.get("Idempotency-Key"),
            "request": _json_or_text(body),
            "status": response.status_code,
            "response": _json_or_text(resp_body),
        })
        return Response(resp_body, response.status_code, dict(response.headers), response.media_type)

    def _injected_failure() -> bool:
        return fail_rate > 0 and rng.random() < fail_rate

    def _set_status(payment_id: str, status: str, reason: str | None = None) -> None:
        ts = _now()
        conn.execute("UPDATE payments SET status = ?, failure_reason = ?, updated_at = ? WHERE payment_id = ?",
                     (status, reason, ts, payment_id))
        conn.execute("INSERT INTO status_history VALUES (?,?,?)", (payment_id, status, ts))

    def _webhook(p: dict) -> None:
        payload = {
            "event_id": f"evt_{uuid.uuid4().hex[:16]}",
            "type": "payment.status_changed",
            "payment_id": p["payment_id"],
            "idempotency_key": p["idempotency_key"],
            "status": p["status"],
            "amount_minor": p["amount_minor"],
            "currency": p["currency"],
            "reference": p["reference"],
            "failure_reason": p["failure_reason"],
            "sent_at": _now(),
        }
        body = json.dumps(payload, sort_keys=True).encode()
        headers = {"Content-Type": "application/json", webhook_sig.HEADER: webhook_sig.sign(secret, body)}
        if webhook_sender is None:
            log.write({"kind": "webhook", "event": payload, "delivered": False, "error": "no webhook URL configured"})
            return
        for attempt in range(1, 4):
            try:
                code = webhook_sender(body, headers)
                log.write({"kind": "webhook", "event": payload, "attempt": attempt, "response_status": code})
                if code < 500:
                    return
            except Exception as e:  # noqa: BLE001 - delivery is best effort; the coordinator also polls
                log.write({"kind": "webhook", "event": payload, "attempt": attempt, "error": repr(e)})
            time.sleep(step)

    def _progress(payment_id: str) -> None:
        """Advances one payment to a final status. State-aware, so it is safe to resume after a restart."""
        with db_lock:
            status = data.payment(conn, payment_id)["status"]
        if status == data.INITIATED:
            time.sleep(step)
            with db_lock:
                p = data.payment(conn, payment_id)
                debtor = data.account(conn, p["debtor_account"])
                if debtor["balance_minor"] < p["amount_minor"]:
                    _set_status(payment_id, data.FAILED, "insufficient_funds")
                else:
                    conn.execute("UPDATE accounts SET balance_minor = balance_minor - ? WHERE id = ?",
                                 (p["amount_minor"], p["debtor_account"]))
                    _set_status(payment_id, data.ACCEPTED)
                conn.commit()
                p = data.payment(conn, payment_id)
            _webhook(p)
            status = p["status"]
        if status == data.ACCEPTED:
            time.sleep(step)
            with db_lock:
                p = data.payment(conn, payment_id)
                conn.execute("UPDATE accounts SET balance_minor = balance_minor + ? WHERE id = ?",
                             (p["amount_minor"], p["creditor_account"]))
                _set_status(payment_id, data.SETTLED)
                conn.commit()
                p = data.payment(conn, payment_id)
            _webhook(p)

    @app.get("/health")
    def health():
        return {"ok": True, "simulated": True}

    @app.get("/accounts")
    def get_accounts():
        with db_lock:
            return data.accounts(conn)

    @app.get("/accounts/{account_id}")
    def get_account(account_id: str):
        with db_lock:
            a = data.account(conn, account_id)
        if a is None:
            raise HTTPException(404, "unknown account")
        return a

    @app.post("/payments", status_code=201)
    def create_payment(req: PaymentRequest, response: Response,
                       idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")):
        if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
            raise HTTPException(400, "Idempotency-Key header (8-128 chars) is required")
        outage_mode = None
        with db_lock:
            if outage["remaining"] > 0:
                outage["remaining"] -= 1
                outage_mode = outage["mode"]
        if outage_mode == "before" or (outage_mode is None and _injected_failure()):
            return JSONResponse({"detail": "service unavailable (injected)"}, status_code=503)

        request_hash = hashlib.sha256(req.model_dump_json().encode()).hexdigest()
        created = False
        with db_lock:
            existing = data.payment_by_key(conn, idempotency_key)
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise HTTPException(409, "Idempotency-Key was already used with a different request")
                p = existing
            else:
                for acc in (req.debtor_account, req.creditor_account):
                    a = data.account(conn, acc)
                    if a is None:
                        raise HTTPException(422, f"unknown account {acc}")
                    if a["currency"] != req.currency:
                        raise HTTPException(422, f"account {acc} is not in {req.currency}")
                if req.debtor_account == req.creditor_account:
                    raise HTTPException(422, "debtor and creditor must differ")
                payment_id = f"pay_{uuid.uuid4().hex[:20]}"
                ts = _now()
                conn.execute(
                    "INSERT INTO payments VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (payment_id, idempotency_key, request_hash, req.debtor_account, req.creditor_account,
                     req.amount_minor, req.currency, req.reference, data.INITIATED, None, ts, ts),
                )
                conn.execute("INSERT INTO status_history VALUES (?,?,?)", (payment_id, data.INITIATED, ts))
                conn.commit()
                p = data.payment(conn, payment_id)
                created = True
        snapshot = _public(p)
        if created:
            executor(lambda: _progress(p["payment_id"]))
        if outage_mode == "after":
            # The payment was accepted but the response is lost: the client must retry with the same key.
            return JSONResponse({"detail": "service unavailable after processing (injected)"}, status_code=503)
        if not created:
            response.status_code = 200
            response.headers["Idempotent-Replayed"] = "true"
        return snapshot

    @app.get("/payments")
    def list_payments():
        with db_lock:
            return [_public(p) for p in data.payments(conn)]

    @app.get("/payments/{payment_id}")
    def get_payment(payment_id: str):
        if _injected_failure():
            return JSONResponse({"detail": "service unavailable (injected)"}, status_code=503)
        with db_lock:
            p = data.payment(conn, payment_id)
        if p is None:
            raise HTTPException(404, "unknown payment")
        return _public(p)

    @app.post("/admin/outage")
    def set_outage(req: OutageRequest, x_admin_token: str | None = Header(default=None)):
        """Failure injection (sandbox only): the next `count` POST /payments calls return 503."""
        token = get_settings().bank_admin_token
        if token and x_admin_token != token:
            raise HTTPException(403, "bad admin token")
        outage.update(remaining=req.count, mode=req.mode)
        return outage

    for p in data.payments(conn):  # resume payments interrupted by a restart
        if p["status"] not in data.FINAL_STATUSES:
            executor(lambda pid=p["payment_id"]: _progress(pid))

    return app


def _public(p: dict) -> dict:
    return {k: v for k, v in p.items() if k != "request_hash"}


def _json_or_text(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")
