"""End-to-end flow over the real HTTP services:
detection -> strategy -> invoice verification -> negotiation -> approvals -> bank -> webhook -> settlement -> ledger.

Starts the supplier and buyer workspaces, bank sandbox and coordinator as separate processes (own DBs under
data/runs/<ts>/), plus a local anvil chain when --ledger anvil.

  python -m services.coordinator.run_flow --offline                 # anvil if installed, else ledger disabled
  python -m services.coordinator.run_flow --offline --ledger none
  python -m services.coordinator.run_flow --ledger configured       # LEDGER_TARGET/RPC_URL/keys from .env
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx

from services.common.config import REPO_ROOT
from services.ledger import local_chain

SERVICES = {
    "supplier": ("services.supplier.app:app", []),
    "buyer": ("services.buyer.app:app", []),
    "bank": ("services.bank_sandbox.app:create_app", ["--factory"]),
    "coordinator": ("services.coordinator.app:create_app", ["--factory"]),
}


def _wait(url: str, timeout: float = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{url} did not start")


def _post(client: httpx.Client, path: str, **kw) -> dict:
    r = client.post(path, **kw)
    if r.status_code >= 400:
        raise SystemExit(f"POST {path} -> {r.status_code}: {r.text}")
    return r.json()


def start_stack(ledger: str, run_dir: Path, ports: dict[str, int], offline: bool) -> tuple[list, dict, dict]:
    """Starts (anvil +) the four services as separate processes. Returns (processes, urls, env)."""
    if ledger == "auto":
        ledger = "anvil" if local_chain.available() else "none"
    run_dir.mkdir(parents=True, exist_ok=True)
    url = {name: f"http://127.0.0.1:{p}" for name, p in ports.items()}
    env = os.environ | {
        "SUPPLIER_DB": str(run_dir / "alpha_foods.db"), "BUYER_DB": str(run_dir / "beta_markets.db"),
        "BANK_DB": str(run_dir / "bank_sandbox.db"), "COORDINATOR_DB": str(run_dir / "coordinator.db"),
        "AUDIT_LOG": str(run_dir / "audit_log.jsonl"), "BANK_LOG": str(run_dir / "bank_sandbox_log.jsonl"),
        "BANK_WEBHOOK_SECRET": os.environ.get("BANK_WEBHOOK_SECRET") or secrets.token_hex(16),
        "BANK_URL": url["bank"], "BANK_WEBHOOK_URL": url["coordinator"] + "/webhooks/bank",
        "SUPPLIER_URL": url["supplier"], "BUYER_URL": url["buyer"], "COORDINATOR_URL": url["coordinator"],
        "BANK_STEP_SECONDS": os.environ.get("BANK_STEP_SECONDS", "0.5"),
    }
    if offline:
        env["LLM_MODE"] = "offline"
    procs: list[subprocess.Popen] = []
    try:
        if ledger == "anvil":
            anvil, rpc = local_chain.start_anvil()
            procs.append(anvil)
            deployment = local_chain.deploy(rpc)
            (REPO_ROOT / "deployments" / "anvil.json").write_text(json.dumps(deployment, indent=2) + "\n")
            s = local_chain.settings_for(rpc)
            env |= {"LEDGER_TARGET": "evm", "RPC_URL": rpc, "CHAIN_ID": str(local_chain.CHAIN_ID),
                    "ADMIN_PRIVATE_KEY": s.admin_private_key, "COORDINATOR_PRIVATE_KEY": s.coordinator_private_key,
                    "SUPPLIER_SIGNER_KEY": s.supplier_signer_key, "BUYER_SIGNER_KEY": s.buyer_signer_key}
        elif ledger == "none":
            env["LEDGER_TARGET"] = "none"
        for name, (target, extra) in SERVICES.items():
            log = (run_dir / f"{name}.log").open("w")
            procs.append(subprocess.Popen(
                [sys.executable, "-m", "uvicorn", target, *extra, "--port", str(ports[name])],
                cwd=REPO_ROOT, env=env, stdout=log, stderr=subprocess.STDOUT))
        for name in SERVICES:
            _wait(url[name] + "/docs")
    except BaseException:
        stop_stack(procs)
        raise
    return procs, url, env


def stop_stack(procs: list) -> None:
    for p in reversed(procs):
        p.terminate()
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="LLM_MODE=offline (deterministic agents)")
    ap.add_argument("--ledger", choices=["auto", "anvil", "none", "configured"], default="auto")
    ap.add_argument("--invoice", default="ALF-2026-0142")
    args = ap.parse_args(argv)

    run_dir = REPO_ROOT / "data" / "runs" / datetime.now().strftime("%Y%m%d-%H%M%S")
    ports = {name: local_chain.free_port() for name in SERVICES}
    procs, url, env = start_stack(args.ledger, run_dir, ports, args.offline)
    try:
        supplier = httpx.Client(base_url=url["supplier"], timeout=120)
        coord = httpx.Client(base_url=url["coordinator"], timeout=300)
        health = coord.get("/health").json()
        print(f"Ledger: {health['ledger_target']} {health['ledger'] or '(disabled: no on-chain records)'}")
        print(f"LLM mode: {env.get('LLM_MODE', 'from .env')}\n")

        risk = _post(supplier, "/agents/risk/run")
        print(f"[Risk Agent]      {risk['message']}")
        strategy = _post(supplier, "/agents/strategy/run")
        print(f"[Strategy Agent]  {strategy['message']}")
        if strategy["recommendation"] != "negotiate_early_payment":
            print("Nothing to negotiate.")
            return 0

        deal = _post(coord, "/invoices/intake", json={"invoice_number": strategy["candidate_invoice"]})
        deal_id = deal["deal_id"]
        print(f"[Coordinator]     invoice {deal['invoice']['number']} verified (buyer signature) -> deal {deal_id}")

        deal = _post(coord, f"/deals/{deal_id}/negotiate")
        neg = deal["negotiation"]
        b = neg.get("bilno", {})
        if b.get("status") == "AGREEMENT":
            print(f"[BILNO]           reference x*={b['x']:,.0f} TL, d*={b['d']:.1%}; evaluations "
                  f"supplier {neg['evaluations']['supplier']}, buyer {neg['evaluations']['buyer']}")
        for t in neg["rounds"]:
            blocked = f" (blocked attempts: {len(t['blocked'])})" if t["blocked"] else ""
            print(f"  round {t['round']} {t['side']:<6} {t['action']:<9} x={t['x']:,.0f} d={t['d']:.1%} "
                  f"[{t['mode']}]{blocked}")
        if deal["state"] != "NEGOTIATED":
            print(f"[Coordinator]     {neg['outcome']} -> recommendation: {neg.get('recommendation')}")
            return 0
        a = deal["agreement"]
        terms = a["agreement"]
        print(f"[Coordinator]     agreement v{a['version']} hash {a['agreement_hash']}")
        print(f"                  pay {terms['pay_amount'] / 100:,.2f} TRY now for {terms['face_amount_x'] / 100:,.2f} "
              f"face ({terms['discount_bps'] / 100:.1f}% discount)")

        for role in ("supplier", "buyer"):
            deal = _post(coord, f"/deals/{deal_id}/sign", json={"role": role})
            print(f"[{role.capitalize() + ' human]':<17}signed EIP-712 agreement (demo key) -> state {deal['state']}")

        deal = _post(coord, f"/deals/{deal_id}/submit")
        print(f"[Coordinator]     bank payment {deal['payment_id']} submitted -> state {deal['state']}")
        deadline = time.time() + 60
        while deal["state"] == "SUBMITTED" and time.time() < deadline:
            time.sleep(0.5)
            deal = coord.get(f"/deals/{deal_id}").json()
        print(f"[Bank -> webhook] payment {deal['payment']['status']}; deal state {deal['state']}\n")

        print("State history:")
        for h in deal["history"]:
            tx = f"  tx {h['ledger_tx']}" if h["ledger_tx"] else ""
            print(f"  {h['ts']}  {str(h['from_state']):<10} -> {h['to_state']:<10} {h['note']}{tx}")
        print("\nLedger writes:")
        for tx in deal["ledger_txs"]:
            print(f"  {tx['action']:<22} {tx['tx_hash'] or '-'}  {tx['note'] or ''}")
        verify = coord.get("/audit/verify").json()
        print(f"\nAudit chain: {'OK' if verify['ok'] else 'BROKEN'} ({verify['message']})")
        print(f"Run artifacts (DBs, audit log, bank sandbox log, service logs): {run_dir}")
        return 0 if deal["state"] == "SETTLED" else 1
    finally:
        stop_stack(procs)


if __name__ == "__main__":
    sys.exit(main())
