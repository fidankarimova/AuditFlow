# Bank sandbox

**Simulated fiat execution. No real bank, no PSD2/licensed provider, no production banking access.**

`services/bank_sandbox/` is a separate FastAPI service with its own SQLite DB (`data/bank_sandbox.db`). It holds
two sandbox accounts with example balances: `SBX-TR-ALPHA-0001` (Alpha Foods, 20,000 TL) and `SBX-TR-BETA-0001`
(Beta Markets, 450,000 TL). Amounts are integer minor units (kuruş).

Run: `make bank` (port 8300) together with `make coordinator` (port 8200), or `make up` for all services.
`BANK_WEBHOOK_SECRET` must be set (in `.env`). The bank refuses to start without it, and the coordinator rejects
every webhook while it is empty.

## API

| Endpoint | Purpose |
|---|---|
| `POST /payments` + `Idempotency-Key` header | Create a payment → `payment_id`, status `INITIATED` (201). Same key + same body → the same payment (200, `Idempotent-Replayed: true`), no second debit. Same key + different body → 409. |
| `GET /payments/{id}`, `GET /payments` | Status + status history (used by the coordinator to poll). |
| `GET /accounts`, `GET /accounts/{id}` | Balances. |
| `POST /admin/outage {count, mode}` | Failure injection: the next `count` `POST /payments` calls return 503, either `before` processing or `after` it (a lost response). Guarded by `BANK_ADMIN_TOKEN` if set. |

Progression (`BANK_STEP_SECONDS` apart): `INITIATED → ACCEPTED` (debtor debited) `→ SETTLED` (creditor credited),
or `INITIATED → FAILED` (`insufficient_funds`). Progression is state-aware and resumes after a restart without a
second debit. `BANK_FAIL_RATE` adds random 503s.

## Webhooks

Each status change is POSTed to `BANK_WEBHOOK_URL` (coordinator `/webhooks/bank`) with
`X-Bank-Signature: t=<unix ts>,v1=<HMAC-SHA256(secret, "<ts>.<raw body>")>` (`services/common/webhook_sig.py`).
The coordinator (`services/coordinator/payments.py`) accepts a webhook only if the signature verifies, the
timestamp is within 5 minutes, the payment is one it initiated, and amount + currency match. Rejected and duplicate
events are recorded in `bank_webhook_events` (shown by `GET /bank/webhook-events`).

The coordinator records each payment it is about to request (by idempotency key) **before** calling the bank, so a
webhook that arrives before the bank's HTTP response is still matched.

## Coordinator retries

`services/coordinator/bank_client.py` retries 429/502/503/504 and transport errors with exponential backoff and
the **same** `Idempotency-Key`, then polls `GET /payments/{id}` until the payment is final. If webhooks are lost,
the polled status can be applied with `PaymentTracker.apply_status`.

## Operation log

Every request/response (including injected 503s) and every webhook delivery attempt is appended to
`evidence/bank_sandbox_log.jsonl` (override with `BANK_LOG`).

## Tests (`tests/test_phase6.py`)

`test_S7_duplicate_bank_request_single_payment`, `test_S8_forged_webhook_rejected`,
`test_S9_bank_outage_safe_retry`, `test_insufficient_funds_fails_without_moving_money`, plus idempotency conflict,
random failure rate, lost webhooks + polling, threaded progression, restart recovery, and fail-closed secrets.
Phase 7 reuses these names in its security test list.
