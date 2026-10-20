import json
import time

import pytest
from fastapi.testclient import TestClient

from services.bank_sandbox import app as bank_app
from services.bank_sandbox import data as bank_data
from services.common import webhook_sig
from services.coordinator import app as coordinator_app
from services.coordinator import payments
from services.coordinator.bank_client import BankClient, BankRejected, BankUnavailable

SECRET = "test-webhook-secret"
AMOUNT = 58_800_00  # 58,800.00 TL in kuruş
REF = "0x" + "ab" * 32  # stands in for the agreement hash


def _inline(fn):
    fn()


class Env:
    """Bank sandbox + coordinator webhook receiver wired together in-process."""

    def __init__(self, tmp_path, webhook_sender=None, executor=_inline, step_seconds=0.0, fail_rate=0.0):
        self.tracker = payments.PaymentTracker(payments.connect(tmp_path / "coordinator.db"), SECRET)
        self.coordinator = TestClient(coordinator_app.create_app(self.tracker))
        self.log_path = tmp_path / "bank_log.jsonl"
        self.db_path = tmp_path / "bank.db"
        self.delivered = []

        def to_coordinator(body, headers):
            r = self.coordinator.post("/webhooks/bank", content=body, headers=headers)
            self.delivered.append((json.loads(body), r.status_code))
            return r.status_code

        self.bank_app = bank_app.create_app(
            db_path=self.db_path, log_path=self.log_path, webhook_secret=SECRET,
            webhook_sender=webhook_sender or to_coordinator, step_seconds=step_seconds,
            fail_rate=fail_rate, executor=executor, seed=7,
        )
        self.bank = TestClient(self.bank_app)
        self.client = BankClient(http=self.bank, backoff_seconds=0, sleep=lambda s: None)

    def balance(self, account):
        return self.bank.get(f"/accounts/{account}").json()["balance_minor"]

    def pay(self, key="agr-1-v1-key", amount=AMOUNT):
        return self.tracker.initiate(self.client, key, bank_data.BETA_ACCOUNT, bank_data.ALPHA_ACCOUNT, amount,
                                     bank_data.CURRENCY, REF)

    def log(self):
        return [json.loads(line) for line in self.log_path.read_text().splitlines()]


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_happy_path_settles_via_verified_webhooks(env):
    beta0, alpha0 = env.balance(bank_data.BETA_ACCOUNT), env.balance(bank_data.ALPHA_ACCOUNT)
    p = env.pay()
    assert p["payment_id"].startswith("pay_")
    assert env.client.attempts[0]["status"] == 201
    final = env.client.wait_for_final(p["payment_id"])
    assert final["status"] == "SETTLED"
    assert [h["status"] for h in final["history"]] == ["INITIATED", "ACCEPTED", "SETTLED"]
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 - AMOUNT
    assert env.balance(bank_data.ALPHA_ACCOUNT) == alpha0 + AMOUNT
    # The coordinator learned SETTLED only through signed webhooks it verified
    assert env.tracker.get(p["payment_id"])["status"] == "SETTLED"
    assert [e["verdict"] for e in env.tracker.events()] == ["accepted", "accepted"]


def test_S7_duplicate_bank_request_single_payment(env):
    beta0 = env.balance(bank_data.BETA_ACCOUNT)
    first = env.pay(key="replay-key-0001")
    body = {"debtor_account": bank_data.BETA_ACCOUNT, "creditor_account": bank_data.ALPHA_ACCOUNT,
            "amount_minor": AMOUNT, "currency": bank_data.CURRENCY, "reference": REF}
    for _ in range(3):
        r = env.bank.post("/payments", json=body, headers={"Idempotency-Key": "replay-key-0001"})
        assert r.status_code == 200 and r.headers["Idempotent-Replayed"] == "true"
        assert r.json()["payment_id"] == first["payment_id"]
    assert len(env.bank.get("/payments").json()) == 1
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 - AMOUNT  # debited once


def test_same_idempotency_key_with_different_body_is_rejected(env):
    env.pay(key="replay-key-0002")
    with pytest.raises(BankRejected) as e:
        env.pay(key="replay-key-0002", amount=AMOUNT + 1)
    assert e.value.status_code == 409
    assert len(env.bank.get("/payments").json()) == 1


def test_idempotency_key_required(env):
    body = {"debtor_account": bank_data.BETA_ACCOUNT, "creditor_account": bank_data.ALPHA_ACCOUNT,
            "amount_minor": AMOUNT, "reference": REF}
    assert env.bank.post("/payments", json=body).status_code == 400
    assert env.bank.get("/payments").json() == []


def test_S8_forged_webhook_rejected(env):
    p = env.pay()
    pid = p["payment_id"]
    env.tracker.conn.execute("UPDATE bank_payments SET status = 'ACCEPTED' WHERE payment_id = ?", (pid,))
    env.tracker.conn.commit()

    def event(**kw):
        return json.dumps({"event_id": "evt_forged", "payment_id": pid, "status": "SETTLED",
                           "amount_minor": AMOUNT, "currency": "TRY", "reference": REF} | kw).encode()

    def post(body, sig):
        return env.coordinator.post("/webhooks/bank", content=body, headers={webhook_sig.HEADER: sig} if sig else {})

    body = event()
    good_sig = webhook_sig.sign(SECRET, body)
    cases = [
        (post(body, None), 401),  # no signature
        (post(body, webhook_sig.sign("attacker-secret", body)), 401),  # wrong key
        (post(event(amount_minor=1), good_sig), 401),  # body changed after signing
        (post(body, webhook_sig.sign(SECRET, body, ts=int(time.time()) - 3600)), 401),  # stale / replayed
        (post(event(payment_id="pay_unknown"), webhook_sig.sign(SECRET, event(payment_id="pay_unknown"))), 404),
        (post(event(amount_minor=1), webhook_sig.sign(SECRET, event(amount_minor=1))), 422),  # wrong amount
    ]
    for response, expected in cases:
        assert response.status_code == expected and response.json()["accepted"] is False
    assert env.tracker.get(pid)["status"] == "ACCEPTED"  # nothing above could settle it
    assert [e["verdict"] for e in env.tracker.events()].count("rejected") == len(cases)

    # The genuine, correctly signed event is accepted once; a redelivery is a no-op duplicate
    assert post(body, good_sig).status_code == 200
    assert post(body, good_sig).json()["accepted"] is True
    assert env.tracker.get(pid)["status"] == "SETTLED"
    assert [e["verdict"] for e in env.tracker.events()][-2:] == ["accepted", "duplicate"]


def test_coordinator_fails_closed_without_secret(tmp_path):
    tracker = payments.PaymentTracker(payments.connect(tmp_path / "c.db"), secret="")
    body = b'{"payment_id": "x"}'
    with pytest.raises(payments.WebhookRejected) as e:
        tracker.handle_webhook(body, webhook_sig.sign("anything", body))
    assert e.value.http_status == 401


def test_S9_bank_outage_safe_retry(env):
    beta0 = env.balance(bank_data.BETA_ACCOUNT)
    # Worst case: the bank processes the payment, then the response is lost (twice), then the bank recovers.
    env.bank.post("/admin/outage", json={"count": 2, "mode": "after"})
    p = env.pay(key="outage-key-0001")
    assert [a["status"] for a in env.client.attempts] == [503, 503, 200]
    assert {a["idempotency_key"] for a in env.client.attempts} == {"outage-key-0001"}  # same key on every retry
    assert len(env.bank.get("/payments").json()) == 1
    assert env.client.wait_for_final(p["payment_id"])["status"] == "SETTLED"
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 - AMOUNT  # exactly one debit

    # Full outage: retries are exhausted, nothing is created; a later retry with the same key pays once.
    env.bank.post("/admin/outage", json={"count": 10, "mode": "before"})
    client = BankClient(http=env.bank, max_attempts=3, backoff_seconds=0, sleep=lambda s: None)
    with pytest.raises(BankUnavailable):
        client.initiate_payment("outage-key-0002", bank_data.BETA_ACCOUNT, bank_data.ALPHA_ACCOUNT, 1_000_00,
                                "TRY", REF)
    assert len(env.bank.get("/payments").json()) == 1
    env.bank.post("/admin/outage", json={"count": 0})
    client.initiate_payment("outage-key-0002", bank_data.BETA_ACCOUNT, bank_data.ALPHA_ACCOUNT, 1_000_00, "TRY", REF)
    client.initiate_payment("outage-key-0002", bank_data.BETA_ACCOUNT, bank_data.ALPHA_ACCOUNT, 1_000_00, "TRY", REF)
    assert len(env.bank.get("/payments").json()) == 2
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 - AMOUNT - 1_000_00

    # Every attempt, including the 503s, is in the sandbox operation log
    statuses = [e["status"] for e in env.log() if e["kind"] == "http" and e["path"] == "/payments"
                and e["method"] == "POST"]
    assert statuses.count(503) == 2 + 3


def test_random_failure_rate_is_retried_safely(tmp_path):
    env = Env(tmp_path, fail_rate=0.5)
    beta0 = env.balance(bank_data.BETA_ACCOUNT)
    env.client.max_attempts = 20
    p = env.pay(key="flaky-key-0001")
    assert any(a["status"] == 503 for a in env.client.attempts)  # seed 7 produces at least one failure
    assert env.client.wait_for_final(p["payment_id"])["status"] == "SETTLED"
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 - AMOUNT


def test_polling_recovers_when_webhooks_are_lost(tmp_path):
    def lost(body, headers):
        raise ConnectionError("coordinator unreachable")

    env = Env(tmp_path, webhook_sender=lost)
    p = env.pay()
    assert env.tracker.get(p["payment_id"])["status"] == "INITIATED"
    final = env.client.wait_for_final(p["payment_id"])
    env.tracker.apply_status(final["payment_id"], final["status"])
    assert env.tracker.get(p["payment_id"])["status"] == "SETTLED"
    assert sum(1 for e in env.log() if e["kind"] == "webhook" and "error" in e) == 6  # 2 events x 3 attempts


def test_insufficient_funds_fails_without_moving_money(env):
    beta0, alpha0 = env.balance(bank_data.BETA_ACCOUNT), env.balance(bank_data.ALPHA_ACCOUNT)
    p = env.pay(key="too-much-0001", amount=beta0 + 1)
    final = env.client.wait_for_final(p["payment_id"])
    assert final["status"] == "FAILED" and final["failure_reason"] == "insufficient_funds"
    assert env.balance(bank_data.BETA_ACCOUNT) == beta0 and env.balance(bank_data.ALPHA_ACCOUNT) == alpha0
    assert env.tracker.get(p["payment_id"])["status"] == "FAILED"


def test_unknown_account_rejected(env):
    with pytest.raises(BankRejected) as e:
        env.client.initiate_payment("bad-account-01", "SBX-NOPE", bank_data.ALPHA_ACCOUNT, 100, "TRY", REF)
    assert e.value.status_code == 422


def test_background_progression_in_threads(tmp_path):
    received = []
    env = Env(tmp_path, webhook_sender=lambda body, headers: received.append(json.loads(body)) or 200,
              executor=bank_app._thread_executor, step_seconds=0.05)
    p = env.client.initiate_payment("thread-key-01", bank_data.BETA_ACCOUNT, bank_data.ALPHA_ACCOUNT, AMOUNT,
                                    "TRY", REF)
    assert p["status"] == "INITIATED"
    final = BankClient(http=env.bank, backoff_seconds=0).wait_for_final(p["payment_id"], timeout=5, poll_seconds=0.02)
    assert final["status"] == "SETTLED"
    deadline = time.time() + 2
    while len(received) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert [e["status"] for e in received] == ["ACCEPTED", "SETTLED"]


def test_restart_resumes_pending_payment_without_double_debit(tmp_path):
    crashed = Env(tmp_path, executor=lambda fn: None)  # bank "crashes" before progressing the payment
    beta0 = crashed.balance(bank_data.BETA_ACCOUNT)
    p = crashed.pay()
    assert crashed.client.get_payment(p["payment_id"])["status"] == "INITIATED"
    restarted = TestClient(bank_app.create_app(db_path=crashed.db_path, log_path=crashed.log_path,
                                               webhook_secret=SECRET, webhook_sender=lambda b, h: 200,
                                               step_seconds=0, executor=_inline))
    assert restarted.get(f"/payments/{p['payment_id']}").json()["status"] == "SETTLED"
    assert restarted.get(f"/accounts/{bank_data.BETA_ACCOUNT}").json()["balance_minor"] == beta0 - AMOUNT
    # A second restart does nothing more
    again = TestClient(bank_app.create_app(db_path=crashed.db_path, log_path=crashed.log_path,
                                           webhook_secret=SECRET, step_seconds=0, executor=_inline))
    assert again.get(f"/accounts/{bank_data.BETA_ACCOUNT}").json()["balance_minor"] == beta0 - AMOUNT


def test_bank_refuses_to_start_without_webhook_secret(tmp_path):
    with pytest.raises(RuntimeError):
        bank_app.create_app(db_path=tmp_path / "b.db", log_path=tmp_path / "l.jsonl", webhook_secret="")


def test_isolation_bank_and_coordinator_never_import_company_workspaces():
    from tests.test_phase4 import _imports

    for package in ("bank_sandbox", "coordinator"):
        assert not any(n.startswith(("services.supplier", "services.buyer")) for n in _imports(package)), package
    assert not any(n.startswith("services.coordinator") for n in _imports("bank_sandbox"))
