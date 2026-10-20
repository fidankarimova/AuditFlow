"""EIP-712 typed data shared off-chain and on-chain (must match contracts/src/*.sol exactly).

- InvoiceRegistry: invoice hash + the buyer's InvoiceConfirmation signature.
- AgreementLedger: the Agreement struct. Its EIP-712 digest IS the agreement_hash that both humans sign (T3),
  so any field change gives a new hash and old signatures stop verifying.

All money values are integer minor units (kuruş). Amounts are example values.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.messages import SignableMessage, encode_typed_data
from eth_utils import keccak, to_checksum_address

BPS = 10_000

AGREEMENT_FIELDS = (
    "agreement_id", "version", "invoice_id", "invoice_hash", "seller", "buyer", "face_amount_x",
    "discount_bps", "pay_amount", "currency", "payee_account", "expires_at", "idempotency_key",
)

_AGREEMENT_TYPE = [
    {"name": "agreementId", "type": "bytes32"},
    {"name": "version", "type": "uint32"},
    {"name": "invoiceId", "type": "uint256"},
    {"name": "invoiceHash", "type": "bytes32"},
    {"name": "seller", "type": "address"},
    {"name": "buyer", "type": "address"},
    {"name": "faceAmount", "type": "uint256"},
    {"name": "discountBps", "type": "uint32"},
    {"name": "payAmount", "type": "uint256"},
    {"name": "currency", "type": "bytes32"},
    {"name": "payeeAccountHash", "type": "bytes32"},
    {"name": "expiresAt", "type": "uint64"},
    {"name": "idempotencyKeyHash", "type": "bytes32"},
]

_CONFIRMATION_TYPE = [
    {"name": "invoiceId", "type": "uint256"},
    {"name": "invoiceHash", "type": "bytes32"},
    {"name": "faceValue", "type": "uint256"},
    {"name": "dueDate", "type": "uint64"},
]

_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]


@dataclass(frozen=True)
class Domain:
    name: str
    chain_id: int
    verifying_contract: str
    version: str = "1"

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "chainId": self.chain_id,
                "verifyingContract": to_checksum_address(self.verifying_contract)}


def registry_domain(chain_id: int, registry: str) -> Domain:
    return Domain("AuditFlow InvoiceRegistry", chain_id, registry)


def ledger_domain(chain_id: int, ledger: str) -> Domain:
    return Domain("AuditFlow AgreementLedger", chain_id, ledger)


def keccak_text(text: str) -> bytes:
    return keccak(text=text)


def to_bytes32(value) -> bytes:
    if isinstance(value, bytes):
        b = value
    else:
        b = bytes.fromhex(value.removeprefix("0x"))
    if len(b) != 32:
        raise ValueError("expected 32 bytes")
    return b


def currency_bytes32(code: str) -> bytes:
    raw = code.encode()
    if not raw or len(raw) > 32:
        raise ValueError("bad currency code")
    return raw.ljust(32, b"\0")


def invoice_hash(seller_tax_id: str, buyer_tax_id: str, number: str, face_minor: int, due_ts: int) -> str:
    """keccak256(abi.encode(string sellerTaxId, string buyerTaxId, string invoiceNumber, uint256, uint64))."""
    encoded = abi_encode(["string", "string", "string", "uint256", "uint64"],
                         [seller_tax_id, buyer_tax_id, number, face_minor, due_ts])
    return "0x" + keccak(encoded).hex()


def pay_amount(face_minor: int, discount_bps: int) -> int:
    """Same integer formula as AgreementLedger.commitAgreement checks on-chain."""
    return face_minor * (BPS - discount_bps) // BPS


def _full(domain: Domain, primary: str, fields: list[dict], message: dict) -> dict:
    return {"types": {"EIP712Domain": _DOMAIN_TYPE, primary: fields}, "primaryType": primary,
            "domain": domain.as_dict(), "message": message}


def agreement_struct(a: dict) -> dict:
    missing = set(AGREEMENT_FIELDS) - set(a)
    if missing:
        raise ValueError(f"agreement missing fields: {sorted(missing)}")
    return {
        "agreementId": keccak_text(a["agreement_id"]),
        "version": a["version"],
        "invoiceId": a["invoice_id"],
        "invoiceHash": to_bytes32(a["invoice_hash"]),
        "seller": to_checksum_address(a["seller"]),
        "buyer": to_checksum_address(a["buyer"]),
        "faceAmount": a["face_amount_x"],
        "discountBps": a["discount_bps"],
        "payAmount": a["pay_amount"],
        "currency": currency_bytes32(a["currency"]),
        "payeeAccountHash": keccak_text(a["payee_account"]),
        "expiresAt": a["expires_at"],
        "idempotencyKeyHash": keccak_text(a["idempotency_key"]),
    }


def agreement_typed_data(domain: Domain, agreement: dict) -> dict:
    return _full(domain, "Agreement", _AGREEMENT_TYPE, agreement_struct(agreement))


def confirmation_typed_data(domain: Domain, invoice_id: int, inv_hash: str, face_minor: int, due_ts: int) -> dict:
    return _full(domain, "InvoiceConfirmation", _CONFIRMATION_TYPE,
                 {"invoiceId": invoice_id, "invoiceHash": to_bytes32(inv_hash), "faceValue": face_minor,
                  "dueDate": due_ts})


def _signable(full: dict) -> SignableMessage:
    return encode_typed_data(full_message=full)


def digest(full: dict) -> str:
    """The 32-byte EIP-712 digest (what the contracts compute with _hashTypedDataV4)."""
    s = _signable(full)
    return "0x" + keccak(b"\x19" + s.version + s.header + s.body).hex()


def sign(private_key: str, full: dict) -> str:
    return "0x" + Account.sign_message(_signable(full), private_key).signature.hex().removeprefix("0x")


def recover(full: dict, signature: str) -> str | None:
    try:
        return Account.recover_message(_signable(full), signature=signature)
    except Exception:  # noqa: BLE001 - malformed signatures simply do not verify
        return None


def address_of(private_key: str) -> str:
    return Account.from_key(private_key).address


def agreement_hash(domain: Domain, agreement: dict) -> str:
    return digest(agreement_typed_data(domain, agreement))
