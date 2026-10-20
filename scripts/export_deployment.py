"""Writes deployments/<network>.json from the forge broadcast file and exports ABIs for Python.

Usage: python scripts/export_deployment.py <chain_id> <network_name>
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.ledger.local_chain import read_broadcast  # noqa: E402

CONTRACTS = ["InvoiceRegistry", "AgreementLedger"]


def main(chain_id: str, network: str) -> None:
    out = read_broadcast(int(chain_id), network)
    (ROOT / "deployments").mkdir(exist_ok=True)
    (ROOT / f"deployments/{network}.json").write_text(json.dumps(out, indent=2) + "\n")

    for name in CONTRACTS:
        artifact = json.loads((ROOT / f"contracts/out/{name}.sol/{name}.json").read_text())
        (ROOT / f"services/ledger/abi/{name}.json").write_text(json.dumps(artifact["abi"], indent=2) + "\n")
    print(json.dumps(out["contracts"], indent=2))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
