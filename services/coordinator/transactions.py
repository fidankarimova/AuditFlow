"""Deterministic Policy & Transaction Coordinator (section 5.5). Plain code, no LLM.

States: VERIFIED -> NEGOTIATED -> APPROVED -> SUBMITTED -> SETTLED; side exits REJECTED, EXPIRED, CANCELLED, FAILED.
- VERIFIED only with the buyer's valid EIP-712 InvoiceConfirmation signature (not the supplier's own flag).
- APPROVED only when BOTH human signatures over the SAME agreement_hash verify. Any field change creates a new
  version with a new hash; the old signatures cannot approve it (S6).
- SUBMITTED: bank payment initiated with the agreement's idempotency key (safe retries, S7/S9).
- SETTLED only after a verified (HMAC) bank webhook for the right payment id and amount (S8).
Every transition is audit-logged and committed to the ledger (or shown as "ledger disabled").
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import date, datetime, timezone

import httpx

from engine import bilno
from services.common import eip712
from services.common.audit import AuditLog, canonical
from services.common.config import Settings, get_settings
from services.common.keys import key_for
from services.coordinator import compliance, negotiation, parties
from services.coordinator.bank_client import BankClient, BankRejected, BankUnavailable
from services.coordinator.payments import PaymentTracker
from services.ledger.client import LedgerClient, LedgerError

VERIFIED, NEGOTIATED, APPROVED, SUBMITTED, SETTLED = "VERIFIED", "NEGOTIATED", "APPROVED", "SUBMITTED", "SETTLED"
REJECTED, EXPIRED, CANCELLED, FAILED = "REJECTED", "EXPIRED", "CANCELLED", "FAILED"

TRANSITIONS = {
    VERIFIED: {NEGOTIATED, REJECTED, CANCELLED},
    NEGOTIATED: {NEGOTIATED, APPROVED, EXPIRED, CANCELLED},
    APPROVED: {NEGOTIATED, SUBMITTED, EXPIRED, CANCELLED},
    SUBMITTED: {SETTLED, FAILED},
}
AGREEMENT_TTL_SECONDS = 24 * 3600
ROLES = ("supplier", "buyer")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_hash TEXT PRIMARY KEY, number TEXT, doc TEXT, ledger_invoice_id INTEGER,
    status TEXT CHECK (status IN ('registered', 'confirmed')), created_at TEXT
);
CREATE TABLE IF NOT EXISTS deals (
    deal_id TEXT PRIMARY KEY, invoice_hash TEXT UNIQUE, invoice_number TEXT, state TEXT,
    negotiation TEXT, payment_id TEXT, created_at TEXT, updated_at TEXT, compliance TEXT
);
CREATE TABLE IF NOT EXISTS agreements (
    deal_id TEXT, version INTEGER, agreement TEXT, agreement_hash TEXT UNIQUE,
    supplier_sig TEXT, buyer_sig TEXT, active INTEGER, PRIMARY KEY (deal_id, version)
);
CREATE TABLE IF NOT EXISTS deal_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT, ts TEXT, from_state TEXT, to_state TEXT,
    actor TEXT, note TEXT, ledger_tx TEXT
);
CREATE TABLE IF NOT EXISTS ledger_txs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, deal_id TEXT, ts TEXT, action TEXT, tx_hash TEXT, explorer_url TEXT,
    note TEXT
);
"""


class CoordinatorError(Exception):
    def __init__(self, http_status: int, message: str, details=None):
        super().__init__(message)
        self.http_status, self.details = http_status, details


class IllegalTransition(CoordinatorError):
    def __init__(self, from_state: str, to_state: str):
        super().__init__(409, f"illegal transition {from_state} -> {to_state}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _keccak_json(obj) -> str:
    return "0x" + eip712.keccak_text(canonical(obj)).hex()


def _due_ts(iso: str) -> int:
    return int(datetime.combine(date.fromisoformat(iso), datetime.min.time(), timezone.utc).timestamp())


class Coordinator:
    def __init__(self, conn: sqlite3.Connection, ledger: LedgerClient, bank: BankClient, tracker: PaymentTracker,
                 supplier: httpx.Client, buyer: httpx.Client, audit: AuditLog, settings: Settings | None = None,
                 clock=time.time):
        self.conn, self.ledger, self.bank, self.tracker = conn, ledger, bank, tracker
        self.supplier, self.buyer, self.audit = supplier, buyer, audit
        self.settings = settings or get_settings()
        self.clock = clock
        conn.executescript(SCHEMA)
        try:  # DBs created before the compliance column existed
            conn.execute("ALTER TABLE deals ADD COLUMN compliance TEXT")
        except sqlite3.OperationalError:
            pass
        self.registry_domain, self.ledger_domain = self._domains()

    def _domains(self):
        addrs = self.ledger.addresses()
        if not addrs:  # ledger disabled: hash under the configured deployment if there is one, else unbound
            try:
                from services.ledger.client import EVM_NETWORKS
                from services.common.config import REPO_ROOT

                net = EVM_NETWORKS.get(self.settings.chain_id, "")
                dep = json.loads((REPO_ROOT / "deployments" / f"{net}.json").read_text())
                addrs = {k: v["address"] for k, v in dep["contracts"].items()}
            except (OSError, KeyError, ValueError):
                addrs = {"InvoiceRegistry": "0x" + "0" * 40, "AgreementLedger": "0x" + "0" * 40}
        return (eip712.registry_domain(self.ledger.chain_id, addrs["InvoiceRegistry"]),
                eip712.ledger_domain(self.ledger.chain_id, addrs["AgreementLedger"]))

    # ------------------------------------------------------------------ helpers

    def _log(self, actor: str, action: str, tier: str, inputs=None, result=None) -> None:
        self.audit.append(actor, action, tier, inputs, result)

    def _ledger(self, deal_id: str | None, action: str, call, *args) -> dict:
        try:
            out = call(*args)
        except LedgerError as e:
            self._log("coordinator", f"ledger:{action}:reverted", "T4", args, {"reason": e.reason})
            self.conn.execute("INSERT INTO ledger_txs (deal_id, ts, action, tx_hash, explorer_url, note) "
                              "VALUES (?,?,?,?,?,?)", (deal_id, _now_iso(), action, None, None, f"reverted: {e.reason}"))
            self.conn.commit()
            raise CoordinatorError(409, f"ledger rejected {action}: {e.reason}") from e
        url = self.ledger.explorer_tx_url(out["tx_hash"]) if out.get("tx_hash") else None
        self.conn.execute("INSERT INTO ledger_txs (deal_id, ts, action, tx_hash, explorer_url, note) VALUES (?,?,?,?,?,?)",
                          (deal_id, _now_iso(), action, out.get("tx_hash"), url, out.get("note")))
        self.conn.commit()
        self._log("coordinator", f"ledger:{action}", "T4", args, {"tx_hash": out.get("tx_hash"), "note": out.get("note")})
        return out

    def _deal_row(self, deal_id: str) -> dict:
        row = self.conn.execute("SELECT * FROM deals WHERE deal_id = ?", (deal_id,)).fetchone()
        if row is None:
            raise CoordinatorError(404, "unknown deal")
        return dict(row)

    def _transition(self, deal_id: str, to_state: str, actor: str, note: str = "", ledger_tx: str | None = None):
        deal = self._deal_row(deal_id)
        if to_state not in TRANSITIONS.get(deal["state"], set()):
            raise IllegalTransition(deal["state"], to_state)
        self.conn.execute("UPDATE deals SET state = ?, updated_at = ? WHERE deal_id = ?", (to_state, _now_iso(), deal_id))
        self.conn.execute("INSERT INTO deal_history (deal_id, ts, from_state, to_state, actor, note, ledger_tx) "
                          "VALUES (?,?,?,?,?,?,?)", (deal_id, _now_iso(), deal["state"], to_state, actor, note, ledger_tx))
        self.conn.commit()
        self._log(actor, f"state:{deal['state']}->{to_state}", "T4", {"deal_id": deal_id}, {"note": note})

    def _agreement(self, deal_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM agreements WHERE deal_id = ? AND active = 1", (deal_id,)).fetchone()
        if row is None:
            return None
        a = dict(row)
        a["agreement"] = json.loads(a["agreement"])
        return a

    def _invoice(self, invoice_hash: str) -> dict:
        row = dict(self.conn.execute("SELECT * FROM invoices WHERE invoice_hash = ?", (invoice_hash,)).fetchone())
        row["doc"] = json.loads(row["doc"])
        return row

    def _check_expiry(self, deal_id: str) -> None:
        a = self._agreement(deal_id)
        if a and a["agreement"]["expires_at"] <= self.clock():
            self._transition(deal_id, EXPIRED, "coordinator", "agreement expired before execution")
            raise CoordinatorError(409, "agreement expired")

    def _store_compliance(self, deal_id: str, subject: str, checks: dict) -> None:
        cur = json.loads(self._deal_row(deal_id)["compliance"] or "[]")
        cur.append({"subject": subject, **checks})
        self.conn.execute("UPDATE deals SET compliance = ? WHERE deal_id = ?", (json.dumps(cur), deal_id))
        self.conn.commit()

    # ------------------------------------------------------------------ 1. invoice intake (-> VERIFIED)

    def confirmation_typed_data(self, ledger_invoice_id: int, invoice_hash: str, doc: dict) -> dict:
        return eip712.confirmation_typed_data(self.registry_domain, ledger_invoice_id, invoice_hash,
                                              round(doc["face"] * 100), _due_ts(doc["due_date"]))

    def intake_invoice(self, invoice_number: str, buyer_confirmation_sig: str | None = None) -> dict:
        """Verify a supplier invoice. Without a signature, the buyer's authorized human confirms with the demo
        key held by this service (documented known risk)."""
        r = self.supplier.get(f"/invoices/{invoice_number}")
        if r.status_code == 404:
            raise CoordinatorError(404, "unknown invoice")
        doc = r.raise_for_status().json()
        checks = compliance.compliance_agent(self.audit).call("check_invoice", {"doc": doc})
        if not checks["ok"]:
            self._log("compliance_agent", "invoice_rejected", "T1", doc, checks)
            raise CoordinatorError(422, "invoice failed compliance checks", checks)
        seller, buyer = parties.get(doc["seller"]), parties.get(doc["buyer"])
        face_minor, due_ts = round(doc["face"] * 100), _due_ts(doc["due_date"])
        inv_hash = eip712.invoice_hash(seller.tax_id, buyer.tax_id, doc["number"], face_minor, due_ts)

        existing = self.conn.execute("SELECT * FROM invoices WHERE invoice_hash = ?", (inv_hash,)).fetchone()
        first_tx_seq = self.conn.execute("SELECT COALESCE(MAX(seq), 0) FROM ledger_txs").fetchone()[0]
        if existing and existing["status"] == "confirmed":
            self._log("coordinator", "invoice_duplicate_rejected", "T4", {"invoice_hash": inv_hash}, None)
            raise CoordinatorError(409, "duplicate invoice: this invoice hash is already registered")

        match = self.buyer.post("/payables/check", json=doc).raise_for_status().json()
        if not match["matches"]:
            self._log("coordinator", "invoice_not_recognised_by_buyer", "T4", doc, match)
            raise CoordinatorError(422, "buyer does not recognise this invoice", match)

        if existing is None:
            reg = self._ledger(None, "registerInvoice", self.ledger.register_invoice, inv_hash,
                               seller.signer(self.settings), buyer.signer(self.settings), face_minor, due_ts)
            self.conn.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?)",
                              (inv_hash, doc["number"], json.dumps(doc), reg["invoice_id"], "registered", _now_iso()))
            self.conn.commit()
            ledger_id = reg["invoice_id"]
        else:
            ledger_id = existing["ledger_invoice_id"]

        typed = self.confirmation_typed_data(ledger_id, inv_hash, doc)
        sig = buyer_confirmation_sig or eip712.sign(key_for("buyer", self.settings), typed)
        signer = eip712.recover(typed, sig)
        if signer != buyer.signer(self.settings):
            self._log("coordinator", "invoice_confirmation_rejected", "T3", {"invoice_hash": inv_hash},
                      {"recovered": signer})
            raise CoordinatorError(422, "invoice is not confirmed by the buyer: bad confirmation signature")
        conf = self._ledger(None, "confirmByBuyerWithSig", self.ledger.confirm_invoice_with_sig, ledger_id, sig)
        self.conn.execute("UPDATE invoices SET status = 'confirmed' WHERE invoice_hash = ?", (inv_hash,))
        deal_id = f"D-{uuid.uuid4().hex[:10]}"
        self.conn.execute("INSERT INTO deals VALUES (?,?,?,?,?,?,?,?,?)",
                          (deal_id, inv_hash, doc["number"], VERIFIED, None, None, _now_iso(), _now_iso(),
                           json.dumps([{"subject": "invoice", **checks}])))
        self.conn.execute("INSERT INTO deal_history (deal_id, ts, from_state, to_state, actor, note, ledger_tx) "
                          "VALUES (?,?,?,?,?,?,?)", (deal_id, _now_iso(), None, VERIFIED, "coordinator",
                                                    "buyer confirmation signature verified", conf.get("tx_hash")))
        self.conn.execute("UPDATE ledger_txs SET deal_id = ? WHERE deal_id IS NULL AND seq > ?", (deal_id, first_tx_seq))
        self.conn.commit()
        self._log("buyer_human", "invoice_confirmed", "T3", {"invoice_hash": inv_hash}, {"signer": signer})
        self._log("coordinator", "state:->VERIFIED", "T4", {"deal_id": deal_id}, {"invoice_hash": inv_hash})
        return self.view(deal_id)

    # ------------------------------------------------------------------ 2. negotiation (-> NEGOTIATED | REJECTED)

    def negotiate(self, deal_id: str) -> dict:
        deal = self._deal_row(deal_id)
        if deal["state"] != VERIFIED:
            raise IllegalTransition(deal["state"], NEGOTIATED)
        t = negotiation.negotiate(self.supplier, self.buyer, deal["invoice_number"], self.settings.allow_partial)
        self.conn.execute("UPDATE deals SET negotiation = ? WHERE deal_id = ?", (json.dumps(t, default=str), deal_id))
        self.conn.commit()
        for turn in t["rounds"]:
            for call in turn["tool_calls"]:
                tier = "T5" if call["status"] == "denied" else "T2"
                self._log(f"{turn['side']}_agent", f"tool:{call['tool']}:{call['status']}", tier, call.get("args"),
                          {"round": turn["round"], "reason": call.get("reason")})
            self._log(f"{turn['side']}_agent", f"negotiation:{turn['action']}", "T2",
                      {"x": turn["x"], "d": turn["d"]}, {"round": turn["round"], "mode": turn["mode"]})
        if t["outcome"] != bilno.AGREEMENT:
            note = f"{t['outcome']}: recommendation {t.get('recommendation')}"
            self._transition(deal_id, REJECTED, "coordinator", note)
            return self.view(deal_id)
        agreement = self._new_agreement(deal, version=1, x=t["agreed"]["x"], d=t["agreed"]["d"])
        self._commit_version(deal_id, agreement, previous=None)
        return self.view(deal_id)

    def _new_agreement(self, deal: dict, version: int, x: float, d: float) -> dict:
        inv = self._invoice(deal["invoice_hash"])
        doc = inv["doc"]
        seller, buyer = parties.get(doc["seller"]), parties.get(doc["buyer"])
        face_minor, bps = round(x * 100), round(d * eip712.BPS)
        agreement_id = f"AGR-{deal['deal_id']}"
        return {
            "agreement_id": agreement_id, "version": version, "invoice_id": inv["ledger_invoice_id"],
            "invoice_hash": deal["invoice_hash"], "seller": seller.signer(self.settings),
            "buyer": buyer.signer(self.settings), "face_amount_x": face_minor, "discount_bps": bps,
            "pay_amount": eip712.pay_amount(face_minor, bps), "currency": doc["currency"],
            "payee_account": seller.bank_account, "expires_at": int(self.clock()) + AGREEMENT_TTL_SECONDS,
            "idempotency_key": f"{agreement_id}-v{version}-{uuid.uuid4().hex[:8]}",
        }

    def _commit_version(self, deal_id: str, agreement: dict, previous: dict | None) -> None:
        doc = self._invoice(self._deal_row(deal_id)["invoice_hash"])["doc"]
        checks = compliance.compliance_agent(self.audit).call(
            "check_agreement", {"agreement": agreement, "doc": doc, "now_ts": int(self.clock())})
        self._store_compliance(deal_id, f"agreement v{agreement['version']}", checks)
        if not checks["ok"]:
            raise CoordinatorError(422, "agreement failed compliance checks", checks)
        a_hash = eip712.agreement_hash(self.ledger_domain, agreement)
        if previous:
            self._ledger(deal_id, "supersede", self.ledger.supersede, previous["agreement_hash"])
            self.conn.execute("UPDATE agreements SET active = 0 WHERE deal_id = ? AND version = ?",
                              (deal_id, previous["version"]))
        out = self._ledger(deal_id, "commitAgreement", self.ledger.commit_agreement, eip712.agreement_struct(agreement))
        if out.get("agreement_hash") and out["agreement_hash"] != a_hash:
            raise CoordinatorError(500, "on-chain agreement hash differs from the off-chain hash")
        self.conn.execute("INSERT INTO agreements VALUES (?,?,?,?,?,?,?)",
                          (deal_id, agreement["version"], json.dumps(agreement), a_hash, None, None, 1))
        self.conn.commit()
        note = f"agreement v{agreement['version']} {a_hash[:12]}..."
        self._transition(deal_id, NEGOTIATED, "coordinator", note, out.get("tx_hash"))

    def amend(self, deal_id: str, face_amount_x: float | None = None, discount: float | None = None,
              actor: str = "coordinator") -> dict:
        """Changes terms => new version, new hash; existing signatures no longer apply (S6)."""
        deal = self._deal_row(deal_id)
        if deal["state"] not in (NEGOTIATED, APPROVED):
            raise IllegalTransition(deal["state"], NEGOTIATED)
        cur = self._agreement(deal_id)
        a = cur["agreement"]
        x = face_amount_x if face_amount_x is not None else a["face_amount_x"] / 100
        d = discount if discount is not None else a["discount_bps"] / eip712.BPS
        neg_id = json.loads(deal["negotiation"])["negotiation_id"]
        for side, client in (("supplier", self.supplier), ("buyer", self.buyer)):
            r = client.post("/evaluate", json={"negotiation_id": neg_id, "invoice_number": deal["invoice_number"],
                                               "x": x, "d": d})
            if r.status_code != 200 or not r.json()["feasible"]:
                raise CoordinatorError(422, f"amended terms are not acceptable to the {side}")
        self._commit_version(deal_id, self._new_agreement(deal, cur["version"] + 1, x, d), previous=cur)
        self._log(actor, "agreement_amended", "T4", {"deal_id": deal_id, "x": x, "d": d}, None)
        return self.view(deal_id)

    # ------------------------------------------------------------------ 3. approvals (T3)

    def typed_agreement(self, deal_id: str) -> dict:
        a = self._agreement(deal_id)
        if a is None:
            raise CoordinatorError(409, "no agreement to sign")
        return eip712.agreement_typed_data(self.ledger_domain, a["agreement"])

    def sign_with_demo_key(self, deal_id: str, role: str) -> dict:
        """The role's authorized human signs in the UI. Demo: the key is held server-side (known risk)."""
        if role not in ROLES:
            raise CoordinatorError(400, "role must be supplier or buyer")
        return self.approve(deal_id, role, eip712.sign(key_for(role, self.settings), self.typed_agreement(deal_id)))

    def approve(self, deal_id: str, role: str, signature: str) -> dict:
        if role not in ROLES:
            raise CoordinatorError(400, "role must be supplier or buyer")
        deal = self._deal_row(deal_id)
        if deal["state"] != NEGOTIATED:
            raise CoordinatorError(409, f"cannot approve in state {deal['state']}")
        self._check_expiry(deal_id)
        a = self._agreement(deal_id)
        expected = a["agreement"]["seller" if role == "supplier" else "buyer"]
        signer = eip712.recover(self.typed_agreement(deal_id), signature)
        if signer != expected:
            self._log(f"{role}_human", "approval_rejected", "T3", {"agreement_hash": a["agreement_hash"]},
                      {"recovered": signer, "expected": expected})
            raise CoordinatorError(422, "signature does not match this agreement version and role")
        if a[f"{role}_sig"]:
            raise CoordinatorError(409, f"{role} already approved this version")
        out = self._ledger(deal_id, f"approve:{role}", self.ledger.approve, a["agreement_hash"], signature)
        self.conn.execute(f"UPDATE agreements SET {role}_sig = ? WHERE deal_id = ? AND version = ?",
                          (signature, deal_id, a["version"]))
        self.conn.commit()
        self._log(f"{role}_human", "agreement_approved", "T3", {"agreement_hash": a["agreement_hash"]},
                  {"signer": signer})
        a = self._agreement(deal_id)
        if a["supplier_sig"] and a["buyer_sig"]:
            self._transition(deal_id, APPROVED, "coordinator", "both signatures verified", out.get("tx_hash"))
        return self.view(deal_id)

    # ------------------------------------------------------------------ 4. execution (T4)

    def submit(self, deal_id: str) -> dict:
        deal = self._deal_row(deal_id)
        if deal["state"] != APPROVED:
            raise IllegalTransition(deal["state"], SUBMITTED)
        self._check_expiry(deal_id)
        a = self._agreement(deal_id)
        if not (a["supplier_sig"] and a["buyer_sig"]):  # defence in depth; APPROVED implies both
            raise CoordinatorError(409, "both approvals are required")
        terms, doc = a["agreement"], self._invoice(deal["invoice_hash"])["doc"]
        checks = compliance.compliance_agent(self.audit).call(
            "check_agreement", {"agreement": terms, "doc": doc, "now_ts": int(self.clock())})
        if not checks["ok"]:
            raise CoordinatorError(422, "agreement failed compliance checks", checks)
        try:
            payment = self.tracker.initiate(self.bank, terms["idempotency_key"], parties.get(doc["buyer"]).bank_account,
                                            terms["payee_account"], terms["pay_amount"], terms["currency"],
                                            a["agreement_hash"])
        except BankUnavailable as e:
            self._log("coordinator", "bank_unavailable", "T4", {"deal_id": deal_id}, {"error": str(e)})
            raise CoordinatorError(503, "bank unavailable; retry submit (same idempotency key)") from e
        except BankRejected as e:
            raise CoordinatorError(422, f"bank rejected the payment: {e.detail}") from e
        self.conn.execute("UPDATE deals SET payment_id = ? WHERE deal_id = ?", (payment["payment_id"], deal_id))
        self.conn.commit()
        out = self._ledger(deal_id, "recordSubmission", self.ledger.record_submission, a["agreement_hash"],
                           "0x" + eip712.keccak_text(payment["payment_id"]).hex())
        self._transition(deal_id, SUBMITTED, "coordinator", f"bank payment {payment['payment_id']}", out.get("tx_hash"))
        self._advance_payment(deal_id)
        return self.view(deal_id)

    def on_payment_status(self, tracked: dict) -> None:
        row = self.conn.execute("SELECT deal_id FROM deals WHERE payment_id = ?", (tracked["payment_id"],)).fetchone()
        if row:
            self._advance_payment(row["deal_id"])

    def _webhook_verified(self, payment_id: str, status: str) -> dict | None:
        row = self.tracker.conn.execute(
            "SELECT * FROM bank_webhook_events WHERE payment_id = ? AND status = ? AND verdict = 'accepted' "
            "ORDER BY seq LIMIT 1", (payment_id, status)).fetchone()
        return dict(row) if row else None

    def _advance_payment(self, deal_id: str) -> None:
        deal = self._deal_row(deal_id)
        if deal["state"] != SUBMITTED or not deal["payment_id"]:
            return
        tracked = self.tracker.get(deal["payment_id"])
        a = self._agreement(deal_id)
        if tracked["status"] == "SETTLED":
            event = self._webhook_verified(deal["payment_id"], "SETTLED")
            if event is None:
                return  # a poll alone never settles; wait for the signed webhook
            receipt = _keccak_json({"payment_id": tracked["payment_id"], "status": "SETTLED",
                                    "amount_minor": tracked["amount_minor"], "currency": tracked["currency"],
                                    "event_id": event["event_id"]})
            try:
                out = self._ledger(deal_id, "recordSettlement", self.ledger.record_settlement, a["agreement_hash"], receipt)
                tx = out.get("tx_hash")
                note = "verified bank webhook"
            except CoordinatorError as e:  # money has moved: record the fact off-chain and flag the ledger gap
                tx, note = None, f"verified bank webhook; LEDGER WRITE FAILED: {e}"
            self._transition(deal_id, SETTLED, "coordinator", note, tx)
        elif tracked["status"] == "FAILED":
            out = self._ledger(deal_id, "recordFailure", self.ledger.record_failure, a["agreement_hash"],
                               _keccak_json({"payment_id": tracked["payment_id"], "status": "FAILED"}))
            self._transition(deal_id, FAILED, "coordinator", "bank payment failed", out.get("tx_hash"))

    def refresh_payment(self, deal_id: str) -> dict:
        """Poll the bank (e.g. webhooks lost). Updates the tracked status; SETTLED still needs the webhook."""
        deal = self._deal_row(deal_id)
        if deal["payment_id"]:
            p = self.bank.get_payment(deal["payment_id"])
            self.tracker.apply_status(p["payment_id"], p["status"])
            self._advance_payment(deal_id)
        return self.view(deal_id)

    def cancel(self, deal_id: str, actor: str) -> dict:
        deal = self._deal_row(deal_id)
        a = self._agreement(deal_id)
        self._transition(deal_id, CANCELLED, actor, f"cancelled in state {deal['state']}")
        if a:
            self._ledger(deal_id, "supersede", self.ledger.supersede, a["agreement_hash"])
        return self.view(deal_id)

    # ------------------------------------------------------------------ views

    def view(self, deal_id: str) -> dict:
        deal = self._deal_row(deal_id)
        inv = self._invoice(deal["invoice_hash"])
        versions = [dict(r) for r in self.conn.execute(
            "SELECT version, agreement, agreement_hash, supplier_sig IS NOT NULL AS supplier_signed, "
            "buyer_sig IS NOT NULL AS buyer_signed, active FROM agreements WHERE deal_id = ? ORDER BY version",
            (deal_id,))]
        for v in versions:
            v["agreement"] = json.loads(v["agreement"])
        return {
            "deal_id": deal_id, "state": deal["state"], "invoice": inv["doc"], "invoice_hash": deal["invoice_hash"],
            "ledger_invoice_id": inv["ledger_invoice_id"], "payment_id": deal["payment_id"],
            "payment": self.tracker.get(deal["payment_id"]) if deal["payment_id"] else None,
            "agreement": next((v for v in versions if v["active"]), None), "versions": versions,
            "negotiation": json.loads(deal["negotiation"]) if deal["negotiation"] else None,
            "compliance": json.loads(deal["compliance"]) if deal["compliance"] else [],
            "history": [dict(r) for r in self.conn.execute(
                "SELECT ts, from_state, to_state, actor, note, ledger_tx FROM deal_history WHERE deal_id = ? "
                "ORDER BY seq", (deal_id,))],
            "ledger_txs": [dict(r) for r in self.conn.execute(
                "SELECT ts, action, tx_hash, explorer_url, note FROM ledger_txs WHERE deal_id = ? ORDER BY seq",
                (deal_id,))],
            "ledger_target": self.ledger.target,
        }

    def deals(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute(
            "SELECT deal_id, invoice_number, state, payment_id, updated_at FROM deals ORDER BY created_at")]
