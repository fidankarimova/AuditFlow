"""Local rehearsal chain: starts anvil and deploys InvoiceRegistry + AgreementLedger with forge.

This is a LOCAL test chain (chain id 31337), not Base Sepolia and not FISCO BCOS. It uses the public anvil
test keys from services/common/keys.py. Used by tests and by `run_flow --ledger anvil`.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

from services.common.config import REPO_ROOT, Settings
from services.common.keys import anvil_key

CONTRACTS_DIR = REPO_ROOT / "contracts"
CHAIN_ID = 31337


def tool(name: str) -> str | None:
    return shutil.which(name) or next(
        (str(p) for p in [Path.home() / ".foundry" / "bin" / name] if p.exists()), None)


def available() -> bool:
    return tool("anvil") is not None and tool("forge") is not None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_anvil(port: int | None = None) -> tuple[subprocess.Popen, str]:
    port = port or free_port()
    proc = subprocess.Popen([tool("anvil"), "--port", str(port), "--chain-id", str(CHAIN_ID), "--silent"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rpc = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc, rpc
        except OSError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("anvil did not start")


def read_broadcast(chain_id: int, network: str) -> dict:
    broadcast = json.loads((CONTRACTS_DIR / f"broadcast/Deploy.s.sol/{chain_id}/run-latest.json").read_text())
    receipts = {r["transactionHash"]: r for r in broadcast["receipts"]}
    contracts, setup_txs = {}, []
    for tx in broadcast["transactions"]:
        receipt = receipts.get(tx["hash"], {})
        entry = {"tx_hash": tx["hash"], "block": int(receipt.get("blockNumber", "0x0"), 16)}
        if tx["transactionType"] == "CREATE":
            contracts[tx["contractName"]] = {"address": tx["contractAddress"], **entry}
        else:
            setup_txs.append({"function": tx.get("function") or "transfer", "to": tx["transaction"]["to"], **entry})
    return {"network": network, "chain_id": chain_id, "contracts": contracts, "setup_transactions": setup_txs}


def deploy(rpc_url: str) -> dict:
    """Runs contracts/script/Deploy.s.sol against a local anvil with the public anvil keys."""
    env = os.environ | {
        "ADMIN_PRIVATE_KEY": anvil_key(0), "COORDINATOR_PRIVATE_KEY": anvil_key(1),
        "SUPPLIER_SIGNER_KEY": anvil_key(2), "BUYER_SIGNER_KEY": anvil_key(3),
    }
    subprocess.run([tool("forge"), "script", "script/Deploy.s.sol", "--rpc-url", rpc_url, "--broadcast"],
                   cwd=CONTRACTS_DIR, env=env, check=True, capture_output=True, text=True)
    return read_broadcast(CHAIN_ID, "anvil")


def settings_for(rpc_url: str, **overrides) -> Settings:
    return Settings(_env_file=None, ledger_target="evm", rpc_url=rpc_url, chain_id=CHAIN_ID,
                    admin_private_key=anvil_key(0), coordinator_private_key=anvil_key(1),
                    supplier_signer_key=anvil_key(2), buyer_signer_key=anvil_key(3), **overrides)
