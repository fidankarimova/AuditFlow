"""Ledger client: one interface, two backends selected by LEDGER_TARGET.

- evm: Base Sepolia, or a local anvil chain for rehearsals/tests (implemented).
- fisco: FISCO BCOS 3.x (interface only; not implemented, see docs/LEDGER.md).
- none: ledger explicitly disabled; writes return tx_hash None (offline tests).

CLI: python -m services.ledger.client ping
"""

from __future__ import annotations

import json
import sys
import time
from abc import ABC, abstractmethod
from pathlib import Path

from services.common.config import REPO_ROOT, Settings, get_settings

ABI_DIR = Path(__file__).parent / "abi"
EVM_NETWORKS = {84532: "base-sepolia", 31337: "anvil"}


class LedgerError(Exception):
    """A ledger call reverted or failed. `reason` is the decoded custom error name when known."""

    def __init__(self, function: str, reason: str, tx_hash: str | None = None):
        super().__init__(f"{function} reverted: {reason}")
        self.function, self.reason, self.tx_hash = function, reason, tx_hash


class LedgerClient(ABC):
    """Operations the coordinator needs from the ledger. Every write returns {"tx_hash": ...}."""

    target: str
    chain_id: int

    @abstractmethod
    def addresses(self) -> dict[str, str]: ...

    @abstractmethod
    def block_number(self) -> int: ...

    @abstractmethod
    def explorer_tx_url(self, tx_hash: str) -> str | None: ...

    # Writes (coordinator key only; agents never hold a key)
    @abstractmethod
    def register_invoice(self, invoice_hash: str, seller: str, buyer: str, face_minor: int, due_ts: int) -> dict: ...

    @abstractmethod
    def confirm_invoice_with_sig(self, invoice_id: int, signature: str) -> dict: ...

    @abstractmethod
    def commit_agreement(self, struct: dict) -> dict: ...

    @abstractmethod
    def approve(self, agreement_hash: str, signature: str) -> dict: ...

    @abstractmethod
    def supersede(self, agreement_hash: str) -> dict: ...

    @abstractmethod
    def record_submission(self, agreement_hash: str, bank_payment_id_hash: str) -> dict: ...

    @abstractmethod
    def record_settlement(self, agreement_hash: str, receipt_hash: str) -> dict: ...

    @abstractmethod
    def record_failure(self, agreement_hash: str, reason_hash: str) -> dict: ...

    # Views
    @abstractmethod
    def hash_agreement(self, struct: dict) -> str: ...

    @abstractmethod
    def agreement_record(self, agreement_hash: str) -> dict: ...


_AGREEMENT_ORDER = ("agreementId", "version", "invoiceId", "invoiceHash", "seller", "buyer", "faceAmount",
                    "discountBps", "payAmount", "currency", "payeeAccountHash", "expiresAt", "idempotencyKeyHash")
_RECORD_FIELDS = ("invoiceId", "version", "expiresAt", "seller", "buyer", "status", "sellerApproved",
                  "buyerApproved", "bankPaymentIdHash", "settlementReceiptHash")
AGREEMENT_STATUS = ("None", "Committed", "Submitted", "Settled", "Failed", "Superseded")


def _b32(value) -> bytes:
    return value if isinstance(value, bytes) else bytes.fromhex(value.removeprefix("0x"))


def _hex(b: bytes) -> str:
    return "0x" + bytes(b).hex()


class EvmLedgerClient(LedgerClient):
    target = "evm"

    def __init__(self, settings: Settings, deployment: dict | None = None, sender_key: str | None = None):
        from web3 import Web3

        self.settings = settings
        self.chain_id = settings.chain_id
        self.network = EVM_NETWORKS.get(settings.chain_id, f"chain-{settings.chain_id}")
        if deployment is None:
            deployment_file = REPO_ROOT / "deployments" / f"{self.network}.json"
            if not deployment_file.exists():
                raise FileNotFoundError(f"{deployment_file} not found; run `make deploy` first")
            deployment = json.loads(deployment_file.read_text())
        self.deployment = deployment
        self.w3 = Web3(Web3.HTTPProvider(settings.rpc_url))
        if self.w3.eth.chain_id != settings.chain_id:
            raise RuntimeError(f"RPC chain id {self.w3.eth.chain_id} != CHAIN_ID {settings.chain_id}")
        self.registry = self._contract("InvoiceRegistry")
        self.ledger = self._contract("AgreementLedger")
        self._errors = self._error_selectors()
        self._sender_key = sender_key or settings.coordinator_private_key

    def _contract(self, name: str):
        abi = json.loads((ABI_DIR / f"{name}.json").read_text())
        address = self.w3.to_checksum_address(self.deployment["contracts"][name]["address"])
        return self.w3.eth.contract(address=address, abi=abi)

    def _error_selectors(self) -> dict[str, str]:
        from eth_utils import keccak

        out = {}
        for c in (self.registry, self.ledger):
            for item in c.abi:
                if item.get("type") == "error":
                    sig = f"{item['name']}({','.join(i['type'] for i in item['inputs'])})"
                    out["0x" + keccak(text=sig)[:4].hex()] = item["name"]
        return out

    def _decode_error(self, e: Exception) -> str:
        data = getattr(e, "data", None)
        if isinstance(data, str) and data.startswith("0x") and len(data) >= 10:
            return self._errors.get(data[:10].lower(), data[:10])
        return getattr(e, "message", None) or str(e)

    def _send(self, name: str, fn) -> dict:
        from eth_account import Account
        from web3.exceptions import ContractCustomError, ContractLogicError

        if not self._sender_key:
            raise LedgerError(name, "COORDINATOR_PRIVATE_KEY is not set")
        acct = Account.from_key(self._sender_key)
        # Public RPCs are load-balanced: the gas estimate can hit a node that has not seen our previous tx yet and
        # revert spuriously. Retry a few times there before treating the revert as real.
        attempts = 1 if self.chain_id == 31337 else 6
        for attempt in range(attempts):
            try:
                tx = fn.build_transaction({
                    "from": acct.address, "chainId": self.chain_id,
                    "nonce": self._next_nonce(acct.address),
                })
                break
            except (ContractCustomError, ContractLogicError) as e:
                if attempt == attempts - 1:
                    raise LedgerError(name, self._decode_error(e)) from e
                time.sleep(2)
        signed = acct.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        self._used_nonce = tx["nonce"]
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt["status"] != 1:
            raise LedgerError(name, "transaction reverted", _hex(tx_hash))
        return {"tx_hash": _hex(tx_hash), "block": receipt["blockNumber"], "receipt": receipt}

    def send_expecting_revert(self, name: str, fn, gas: int = 500_000) -> dict:
        """Evidence helper for attack runs: sends the call anyway (fixed gas, no estimate) so the revert is mined
        and has a tx hash. Returns {tx_hash, block, status (0 = reverted), reason (decoded from a dry run)}."""
        from eth_account import Account
        from web3.exceptions import ContractCustomError, ContractLogicError

        acct = Account.from_key(self._sender_key)
        reason = None
        try:
            fn.call({"from": acct.address})
        except (ContractCustomError, ContractLogicError) as e:
            reason = self._decode_error(e)
        tx = fn.build_transaction({"from": acct.address, "chainId": self.chain_id, "gas": gas,
                                   "nonce": self._next_nonce(acct.address)})
        tx_hash = self.w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction)
        self._used_nonce = tx["nonce"]
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return {"function": name, "tx_hash": _hex(tx_hash), "block": receipt["blockNumber"],
                "status": receipt["status"], "reason": reason}

    def _next_nonce(self, address: str) -> int:
        # never reuse a nonce even if a lagging RPC node reports an old pending count
        chain = self.w3.eth.get_transaction_count(address, "pending")
        return max(chain, getattr(self, "_used_nonce", -1) + 1)

    def addresses(self) -> dict[str, str]:
        return {name: c["address"] for name, c in self.deployment["contracts"].items()}

    def block_number(self) -> int:
        return self.w3.eth.block_number

    def explorer_tx_url(self, tx_hash: str) -> str | None:
        if self.settings.chain_id == 84532:
            return f"https://sepolia.basescan.org/tx/{tx_hash}"
        return None

    @staticmethod
    def _public(out: dict, **extra) -> dict:
        return {"tx_hash": out["tx_hash"], "block": out["block"], **extra}

    def register_invoice(self, invoice_hash, seller, buyer, face_minor, due_ts) -> dict:
        cs = self.w3.to_checksum_address
        out = self._send("registerInvoice", self.registry.functions.registerInvoice(
            _b32(invoice_hash), cs(seller), cs(buyer), face_minor, due_ts))
        events = self.registry.events.InvoiceRegistered().process_receipt(out["receipt"])
        return self._public(out, invoice_id=events[0]["args"]["invoiceId"])

    def confirm_invoice_with_sig(self, invoice_id, signature) -> dict:
        return self._public(self._send("confirmByBuyerWithSig", self.registry.functions.confirmByBuyerWithSig(
            invoice_id, bytes.fromhex(signature.removeprefix("0x")))))

    def _struct(self, struct: dict) -> tuple:
        return tuple(struct[k] for k in _AGREEMENT_ORDER)

    def commit_agreement(self, struct) -> dict:
        out = self._send("commitAgreement", self.ledger.functions.commitAgreement(self._struct(struct)))
        events = self.ledger.events.AgreementCommitted().process_receipt(out["receipt"])
        return self._public(out, agreement_hash=_hex(events[0]["args"]["agreementHash"]))

    def approve(self, agreement_hash, signature) -> dict:
        return self._public(self._send("approve", self.ledger.functions.approve(
            _b32(agreement_hash), bytes.fromhex(signature.removeprefix("0x")))))

    def supersede(self, agreement_hash) -> dict:
        return self._public(self._send("supersede", self.ledger.functions.supersede(_b32(agreement_hash))))

    def record_submission(self, agreement_hash, bank_payment_id_hash) -> dict:
        return self._public(self._send("recordSubmission", self.ledger.functions.recordSubmission(
            _b32(agreement_hash), _b32(bank_payment_id_hash))))

    def record_settlement(self, agreement_hash, receipt_hash) -> dict:
        return self._public(self._send("recordSettlement", self.ledger.functions.recordSettlement(
            _b32(agreement_hash), _b32(receipt_hash))))

    def record_failure(self, agreement_hash, reason_hash) -> dict:
        return self._public(self._send("recordFailure", self.ledger.functions.recordFailure(
            _b32(agreement_hash), _b32(reason_hash))))

    def hash_agreement(self, struct) -> str:
        return _hex(self.ledger.functions.hashAgreement(self._struct(struct)).call())

    def agreement_record(self, agreement_hash) -> dict:
        r = dict(zip(_RECORD_FIELDS, self.ledger.functions.getRecord(_b32(agreement_hash)).call()))
        r["status"] = AGREEMENT_STATUS[r["status"]]
        for k in ("bankPaymentIdHash", "settlementReceiptHash"):
            r[k] = _hex(r[k])
        return r

    def invoice_status(self, invoice_id: int) -> str:
        return ("None", "Registered", "Confirmed", "Settled", "Cancelled")[
            self.registry.functions.statusOf(invoice_id).call()]


class NoLedger(LedgerClient):
    """Explicitly disabled ledger (LEDGER_TARGET=none). Writes nothing and says so: every result has
    tx_hash None. Used for offline tests and runs without a chain; never presented as an on-chain record."""

    target = "none"

    def __init__(self, settings: Settings | None = None):
        self.chain_id = (settings or get_settings()).chain_id
        self._next_invoice = 0

    def addresses(self) -> dict[str, str]:
        return {}

    def block_number(self) -> int:
        return 0

    def explorer_tx_url(self, tx_hash: str) -> str | None:
        return None

    def _none(self, **extra) -> dict:
        return {"tx_hash": None, "note": "ledger disabled (LEDGER_TARGET=none)", **extra}

    def register_invoice(self, invoice_hash, seller, buyer, face_minor, due_ts) -> dict:
        self._next_invoice += 1
        return self._none(invoice_id=self._next_invoice)

    def confirm_invoice_with_sig(self, invoice_id, signature) -> dict:
        return self._none()

    def commit_agreement(self, struct) -> dict:
        return self._none()

    def approve(self, agreement_hash, signature) -> dict:
        return self._none()

    def supersede(self, agreement_hash) -> dict:
        return self._none()

    def record_submission(self, agreement_hash, bank_payment_id_hash) -> dict:
        return self._none()

    def record_settlement(self, agreement_hash, receipt_hash) -> dict:
        return self._none()

    def record_failure(self, agreement_hash, reason_hash) -> dict:
        return self._none()

    def hash_agreement(self, struct) -> str:
        raise NotImplementedError("no ledger")

    def agreement_record(self, agreement_hash) -> dict:
        return {}


class FiscoLedgerClient(LedgerClient):
    """Placeholder for FISCO BCOS 3.x. Deliberately not implemented: we deploy to Base Sepolia."""

    target = "fisco"

    def __new__(cls, *args, **kwargs):
        raise NotImplementedError("FISCO BCOS backend is not implemented; use LEDGER_TARGET=evm")


def get_ledger_client(settings: Settings | None = None) -> LedgerClient:
    settings = settings or get_settings()
    backends = {"evm": EvmLedgerClient, "fisco": FiscoLedgerClient, "none": NoLedger}
    return backends[settings.ledger_target](settings)


def main(argv: list[str]) -> int:
    if argv[:1] != ["ping"]:
        print("usage: python -m services.ledger.client ping")
        return 2
    client = get_ledger_client()
    registry = getattr(client, "registry", None)
    ledger = getattr(client, "ledger", None)
    print(f"target:   {client.target} ({getattr(client, 'network', '')})")
    for name, addr in client.addresses().items():
        print(f"{name}: {addr}")
    print(f"block:    {client.block_number()}")
    if registry is not None and ledger is not None:
        print(f"ledger.registry() -> {ledger.functions.registry().call()}")
        print(f"registry.nextInvoiceId() -> {registry.functions.nextInvoiceId().call()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
