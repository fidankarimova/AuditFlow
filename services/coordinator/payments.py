"""Coordinator's record of bank payments and its bank-webhook verifier.

The coordinator records every payment it is about to request (keyed by its idempotency key) BEFORE calling the
bank, so a webhook that arrives before the bank's HTTP response is still matched. A webhook is accepted only if
(1) its HMAC-SHA256 signature over the raw body verifies with the shared secret and its timestamp is fresh,
(2) it refers to a payment the coordinator itself initiated (payment id, or idempotency key not yet bound to one),
and (3) amount and currency match what the coordinator asked for. Everything else is rejected and recorded. Duplicate events are ignored.
A payment counts as SETTLED for the coordinator only through a verified webhook (or a verified poll of the bank).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from services.common import webhook_sig
from services.common.db import DATA_DIR
from services.coordinator.bank_client import BankClient

DB_PATH = DATA_DIR / "coordinator.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_payments (
    idempotency_key TEXT PRIMARY KEY, payment_id TEXT UNIQUE, reference TEXT,
    amount_minor INTEGER, currency TEXT, status TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS bank_webhook_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT, event_id TEXT, payment_id TEXT, status TEXT,
    verdict TEXT CHECK (verdict IN ('accepted', 'duplicate', 'rejected')), reason TEXT, raw TEXT
);
"""

# Allowed forward moves of a payment as seen by the coordinator (out-of-order webhooks never move it backwards).
_ORDER = {"REQUESTED": 0, "INITIATED": 1, "ACCEPTED": 2, "SETTLED": 3, "FAILED": 3}


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


class WebhookRejected(Exception):
    def __init__(self, http_status: int, reason: str):
        super().__init__(reason)
        self.http_status, self.reason = http_status, reason


class PaymentTracker:
    def __init__(self, conn: sqlite3.Connection, secret: str,
                 on_status: Callable[[dict], None] | None = None, clock: Callable[[], float] | None = None):
        self.conn, self.secret, self.on_status, self.clock = conn, secret, on_status, clock

    def expect(self, idempotency_key: str, reference: str, amount_minor: int, currency: str) -> None:
        """Records a payment the coordinator is about to request. Called before the bank is contacted."""
        self.conn.execute(
            "INSERT OR IGNORE INTO bank_payments VALUES (?,?,?,?,?,?,?)",
            (idempotency_key, None, reference, amount_minor, currency, "REQUESTED", _now()),
        )
        self.conn.commit()

    def bind(self, idempotency_key: str, payment: dict) -> dict:
        """Links the bank's payment id to the expected payment, after checking the bank echoed the right terms."""
        tracked = self.get_by_key(idempotency_key)
        if tracked is None:
            raise ValueError(f"no expected payment for key {idempotency_key}")
        if (payment["amount_minor"], payment["currency"], payment["reference"]) != (
                tracked["amount_minor"], tracked["currency"], tracked["reference"]):
            raise ValueError("bank payment terms differ from the requested payment")
        if tracked["payment_id"] not in (None, payment["payment_id"]):
            raise ValueError("idempotency key is already bound to another payment")
        self.conn.execute("UPDATE bank_payments SET payment_id = ? WHERE idempotency_key = ?",
                          (payment["payment_id"], idempotency_key))
        self.conn.commit()
        return self.apply_status(payment["payment_id"], payment["status"])

    def initiate(self, client: BankClient, idempotency_key: str, debtor_account: str, creditor_account: str,
                 amount_minor: int, currency: str, reference: str) -> dict:
        """expect -> POST /payments (retried with the same key) -> bind. Safe to call again with the same key."""
        self.expect(idempotency_key, reference, amount_minor, currency)
        payment = client.initiate_payment(idempotency_key, debtor_account, creditor_account, amount_minor,
                                          currency, reference)
        return self.bind(idempotency_key, payment)

    def get(self, payment_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM bank_payments WHERE payment_id = ?", (payment_id,)).fetchone()
        return dict(row) if row else None

    def get_by_key(self, idempotency_key: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM bank_payments WHERE idempotency_key = ?",
                                (idempotency_key,)).fetchone()
        return dict(row) if row else None

    def events(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM bank_webhook_events ORDER BY seq")]

    def _record(self, event: dict, verdict: str, reason: str, raw: bytes) -> None:
        self.conn.execute(
            "INSERT INTO bank_webhook_events (received_at, event_id, payment_id, status, verdict, reason, raw) "
            "VALUES (?,?,?,?,?,?,?)",
            (_now(), event.get("event_id"), event.get("payment_id"), event.get("status"), verdict, reason,
             raw.decode(errors="replace")[:2000]),
        )
        self.conn.commit()

    def _reject(self, event: dict, http_status: int, reason: str, raw: bytes):
        self._record(event, "rejected", reason, raw)
        raise WebhookRejected(http_status, reason)

    def handle_webhook(self, raw: bytes, signature: str | None) -> dict:
        """Verifies and applies one webhook. Returns the tracked payment; raises WebhookRejected otherwise."""
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError
        except ValueError:
            event = {}
        try:
            webhook_sig.verify(self.secret, raw, signature, now=self.clock() if self.clock else None)
        except webhook_sig.SignatureError as e:
            self._reject(event, 401, f"bad signature: {e}", raw)
        tracked = self.get(event.get("payment_id", ""))
        if tracked is None:
            # Webhook raced ahead of the bank's HTTP response: match the not-yet-bound expected payment by key.
            pending = self.get_by_key(event.get("idempotency_key") or "")
            if pending is None or pending["payment_id"] is not None:
                self._reject(event, 404, "unknown payment id", raw)
            tracked = pending
        if event.get("amount_minor") != tracked["amount_minor"] or event.get("currency") != tracked["currency"]:
            self._reject(event, 422, "amount/currency does not match the initiated payment", raw)
        if event.get("status") not in _ORDER or event["status"] == "REQUESTED":
            self._reject(event, 422, "unknown status", raw)
        seen = self.conn.execute(
            "SELECT 1 FROM bank_webhook_events WHERE event_id = ? AND verdict = 'accepted'", (event.get("event_id"),)
        ).fetchone()
        if seen:
            self._record(event, "duplicate", "event already processed", raw)
            return tracked
        self._record(event, "accepted", "signature, payment id and amount verified", raw)
        if tracked["payment_id"] is None:
            self.conn.execute("UPDATE bank_payments SET payment_id = ? WHERE idempotency_key = ?",
                              (event["payment_id"], tracked["idempotency_key"]))
            self.conn.commit()
        return self.apply_status(event["payment_id"], event["status"])

    def apply_status(self, payment_id: str, status: str) -> dict:
        """Moves a tracked payment forward (used by verified webhooks and by polling the bank)."""
        tracked = self.get(payment_id)
        if _ORDER[status] > _ORDER[tracked["status"]]:
            self.conn.execute("UPDATE bank_payments SET status = ?, updated_at = ? WHERE payment_id = ?",
                              (status, _now(), payment_id))
            self.conn.commit()
            tracked = self.get(payment_id)
            if self.on_status:
                self.on_status(tracked)
        return tracked


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
