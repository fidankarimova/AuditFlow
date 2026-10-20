"""Signing keys per role.

Real keys come from the environment (.env). When a key is not set, a well-known PUBLIC test key is derived from
Anvil's default mnemonic so the offline demo runs out of the box. Those keys are for local/test chains only and
must never hold real funds. Human approver keys being held server-side at all is a documented known risk.
"""

from __future__ import annotations

from functools import lru_cache

from eth_account import Account

from services.common.config import Settings, get_settings

ANVIL_MNEMONIC = "test test test test test test test test test test test junk"
ROLE_INDEX = {"admin": 0, "coordinator": 1, "supplier": 2, "buyer": 3}


@lru_cache
def anvil_key(index: int) -> str:
    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(ANVIL_MNEMONIC, account_path=f"m/44'/60'/0'/0/{index}")
    return "0x" + acct.key.hex().removeprefix("0x")


def key_for(role: str, settings: Settings | None = None) -> str:
    s = settings or get_settings()
    configured = {
        "admin": s.admin_private_key,
        "coordinator": s.coordinator_private_key,
        "supplier": s.supplier_signer_key,
        "buyer": s.buyer_signer_key,
    }[role]
    return configured or anvil_key(ROLE_INDEX[role])


def is_demo_key(role: str, settings: Settings | None = None) -> bool:
    return key_for(role, settings) == anvil_key(ROLE_INDEX[role])


def address_for(role: str, settings: Settings | None = None) -> str:
    return Account.from_key(key_for(role, settings)).address
