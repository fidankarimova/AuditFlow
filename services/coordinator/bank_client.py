"""Coordinator-side client for the bank sandbox.

Safe retries: every retry of a payment reuses the SAME Idempotency-Key, so an outage (including a lost response
after the bank already accepted the payment) can never produce a second payment. Retries use exponential backoff;
after initiation the coordinator polls GET /payments/{id} until the payment is final.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from services.common.config import get_settings

RETRYABLE_STATUS = {429, 502, 503, 504}
FINAL_STATUSES = ("SETTLED", "FAILED")


class BankUnavailable(Exception):
    """The bank did not answer successfully after all retries. The payment may or may not exist: retry later
    with the same idempotency key, never with a new one."""


class BankRejected(Exception):
    def __init__(self, status_code: int, detail):
        super().__init__(f"bank rejected the request ({status_code}): {detail}")
        self.status_code, self.detail = status_code, detail


class BankClient:
    def __init__(
        self,
        base_url: str | None = None,
        http: httpx.Client | None = None,
        max_attempts: int = 5,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http = http or httpx.Client(base_url=base_url or get_settings().bank_url, timeout=10)
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.attempts: list[dict] = []  # evidence: every attempt with its key and outcome

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        last_error = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                r = self.http.request(method, path, **kwargs)
                self.attempts.append({"method": method, "path": path, "attempt": attempt, "status": r.status_code,
                                      "idempotency_key": kwargs.get("headers", {}).get("Idempotency-Key")})
                if r.status_code not in RETRYABLE_STATUS:
                    return r
                last_error = f"HTTP {r.status_code}"
            except httpx.TransportError as e:
                self.attempts.append({"method": method, "path": path, "attempt": attempt, "error": repr(e)})
                last_error = repr(e)
            if attempt < self.max_attempts:
                self.sleep(self.backoff_seconds * 2 ** (attempt - 1))
        raise BankUnavailable(f"{method} {path} failed after {self.max_attempts} attempts: {last_error}")

    @staticmethod
    def _json(r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise BankRejected(r.status_code, r.json().get("detail") if r.content else None)
        return r.json()

    def initiate_payment(
        self, idempotency_key: str, debtor_account: str, creditor_account: str,
        amount_minor: int, currency: str, reference: str,
    ) -> dict:
        body = {"debtor_account": debtor_account, "creditor_account": creditor_account,
                "amount_minor": amount_minor, "currency": currency, "reference": reference}
        r = self._request("POST", "/payments", json=body, headers={"Idempotency-Key": idempotency_key})
        return self._json(r)

    def get_payment(self, payment_id: str) -> dict:
        return self._json(self._request("GET", f"/payments/{payment_id}"))

    def wait_for_final(self, payment_id: str, timeout: float = 30, poll_seconds: float = 0.5) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            p = self.get_payment(payment_id)
            if p["status"] in FINAL_STATUSES or time.monotonic() >= deadline:
                return p
            self.sleep(poll_seconds)
