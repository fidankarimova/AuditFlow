"""End-to-end demo run that produces the execution evidence (Phase 9).

Scenario A (deal): register + buyer-confirm the invoice on-chain -> stress test -> strategy -> negotiation ->
commit -> both approvals -> [attack: change amount after approval] -> re-approval -> bank payment -> signed webhook
-> recordSettlement; then attacks: duplicate invoice (on-chain revert), replayed bank request, forged webhook,
agent over-limit offer.
Scenario B (no deal): buyer reserve 440,000 -> NO_FEASIBLE_AGREEMENT -> bank loan recommended.

Writes docs/EXECUTION_EVIDENCE.md and copies logs into evidence/.

  python scripts/run_demo_scenario.py                      # local anvil chain (needs Foundry)
  python scripts/run_demo_scenario.py --ledger configured  # LEDGER_TARGET/RPC_URL/keys from .env (Base Sepolia)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.buyer import data as buyer_data  # noqa: E402
from services.common import eip712  # noqa: E402
from services.common.config import Settings, get_settings  # noqa: E402
from services.common.keys import key_for  # noqa: E402
from services.common.llm import ScriptedBackend  # noqa: E402
from services.coordinator.run_flow import SERVICES, start_stack, stop_stack  # noqa: E402
from services.ledger import local_chain  # noqa: E402
from services.ledger.client import EvmLedgerClient  # noqa: E402
from services.supplier import agents as supplier_agents  # noqa: E402
from services.supplier import data as supplier_data  # noqa: E402

EVIDENCE = ROOT / "evidence"
DOC = ROOT / "docs" / "EXECUTION_EVIDENCE.md"


class Recorder:
    """Runs steps and records actor, result, ledger txs, bank payment id and log line references."""

    def __init__(self, coord: httpx.Client, run_dir: Path, ledger: EvmLedgerClient | None):
        self.coord, self.run_dir, self.ledger, self.rows, self.out = coord, run_dir, ledger, [], []

    def _audit_seq(self) -> int:
        entries = self.coord.get("/audit/entries", params={"limit": 100_000}).json()
        return entries[-1]["seq"] if entries else 0

    def _bank_lines(self) -> int:
        p = self.run_dir / "bank_sandbox_log.jsonl"
        return len(p.read_text().splitlines()) if p.exists() else 0

    def log(self, text: str) -> None:
        print(text)
        self.out.append(text)

    def step(self, scenario: str, name: str, actor: str, fn):
        a0, b0 = self._audit_seq(), self._bank_lines()
        result, txs, payment = fn()
        a1, b1 = self._audit_seq(), self._bank_lines()
        self.rows.append({"scenario": scenario, "step": name, "actor": actor, "result": result, "txs": txs or [],
                          "payment": payment, "audit": (a0 + 1, a1) if a1 > a0 else None,
                          "bank": (b0 + 1, b1) if b1 > b0 else None})
        self.log(f"[{scenario}] {name} ({actor}): {result}" + (f"  txs={[t['tx'] for t in txs]}" if txs else ""))

    def tx_link(self, tx: str | None) -> str:
        if not tx:
            return "-"
        url = self.ledger.explorer_tx_url(tx) if self.ledger else None
        return f"[`{tx[:12]}…`]({url})" if url else f"`{tx}`"


def _ok(r: httpx.Response) -> dict:
    if r.status_code >= 400:
        raise SystemExit(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text}")
    return r.json()


def _new_txs(deal: dict, before: int, label: str | None = None) -> list[dict]:
    return [{"tx": t["tx_hash"], "action": t["action"], "note": t["note"]} for t in deal["ledger_txs"][before:]
            if label is None or t["action"].startswith(label)]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", choices=["anvil", "configured"], default="anvil")
    args = ap.parse_args()

    started = datetime.now(timezone.utc)
    run_root = ROOT / "data" / "runs" / f"evidence-{started:%Y%m%d-%H%M%S}"
    series = f"E{started:%m%d%H%M%S}"  # fresh invoice hashes on a persistent chain
    procs = []
    try:
        if args.ledger == "anvil":
            if not local_chain.available():
                raise SystemExit("anvil/forge not found: install Foundry or use --ledger configured")
            anvil, rpc = local_chain.start_anvil()
            procs.append(anvil)
            deployment = local_chain.deploy(rpc)
            (ROOT / "deployments" / "anvil.json").write_text(json.dumps(deployment, indent=2) + "\n")
            settings = local_chain.settings_for(rpc)
            os.environ.update({"LEDGER_TARGET": "evm", "RPC_URL": rpc, "CHAIN_ID": str(local_chain.CHAIN_ID),
                               "ADMIN_PRIVATE_KEY": settings.admin_private_key,
                               "COORDINATOR_PRIVATE_KEY": settings.coordinator_private_key,
                               "SUPPLIER_SIGNER_KEY": settings.supplier_signer_key,
                               "BUYER_SIGNER_KEY": settings.buyer_signer_key})
            ledger = EvmLedgerClient(settings, deployment=deployment)
            ledger_label = f"local anvil chain (chain id {local_chain.CHAIN_ID}), NOT a public testnet"
        else:
            settings = get_settings()
            if settings.ledger_target != "evm":
                raise SystemExit("--ledger configured needs LEDGER_TARGET=evm in .env")
            ledger = EvmLedgerClient(settings)
            ledger_label = f"{ledger.network} (chain id {settings.chain_id})"
        os.environ["INVOICE_SERIES"] = series

        # ------------------------------------------------------------------ scenario A
        run_a = run_root / "A_deal"
        ports = {n: local_chain.free_port() for n in SERVICES}
        stack, url, _ = start_stack("configured", run_a, ports, offline=True)
        procs += stack
        supplier = httpx.Client(base_url=url["supplier"], timeout=120)
        bank = httpx.Client(base_url=url["bank"], timeout=60)
        coord = httpx.Client(base_url=url["coordinator"], timeout=300)
        rec = Recorder(coord, run_a, ledger)
        rec.log(f"Ledger: {ledger_label}; contracts {ledger.addresses()}")
        invoice = supplier_data.invoice_number("0142")
        state = {}

        def risk():
            r = _ok(supplier.post("/agents/risk/run"))
            return f"crisis={r['crisis']}, {r['message']}", None, None

        def strategy():
            r = _ok(supplier.post("/agents/strategy/run"))
            return (f"{r['recommendation']} for {r['candidate_invoice']}; best alternative without a deal: "
                    f"{r['best_alternative_without_deal']}"), None, None

        def intake():
            d = _ok(coord.post("/invoices/intake", json={"invoice_number": invoice}))
            state["deal"] = d["deal_id"]
            return (f"VERIFIED: on-chain invoice id {d['ledger_invoice_id']}, hash {d['invoice_hash'][:14]}…",
                    _new_txs(d, 0), None)

        def negotiate():
            d = _ok(coord.post(f"/deals/{state['deal']}/negotiate"))
            n, a = d["negotiation"], d["agreement"]
            state["v1_hash"], state["v1_typed"] = a["agreement_hash"], None
            moves = " → ".join(f"{t['side'][0].upper()}:{t['action']} {t['d']:.1%}" for t in n["rounds"])
            return (f"{n['outcome']}: x*={n['bilno']['x']:,.0f} d*={n['bilno']['d']:.1%}; rounds {moves}; "
                    f"agreement v1 {a['agreement_hash'][:14]}…", _new_txs(d, 2), None)

        def sign(role):
            def run():
                before = len(_ok(coord.get(f"/deals/{state['deal']}"))["ledger_txs"])
                if role == "supplier" and "v1_sig" not in state:  # keep the v1 signature for the S6 attack
                    typed = _typed(coord, state["deal"])
                    state["v1_sig"] = eip712.sign(key_for("supplier", settings), typed)
                    state["v1_sig_valid"] = (eip712.recover(typed, state["v1_sig"]) == typed["message"]["seller"]
                                             and eip712.digest(typed) == state["v1_hash"])
                d = _ok(coord.post(f"/deals/{state['deal']}/sign", json={"role": role}))
                v = d["agreement"]["version"]
                return f"EIP-712 signature on v{v} verified → state {d['state']}", _new_txs(d, before), None
            return run

        def amend(x, label):
            def run():
                before = len(_ok(coord.get(f"/deals/{state['deal']}"))["ledger_txs"])
                d = _ok(coord.post(f"/deals/{state['deal']}/amend", json={"face_amount_x": x}))
                a = d["agreement"]
                return (f"{label}: v{a['version']} hash {a['agreement_hash'][:14]}…, signatures cleared, state "
                        f"{d['state']}"), _new_txs(d, before), None
            return run

        def reuse_old_approval():
            r = coord.post(f"/deals/{state['deal']}/approve", json={"role": "supplier", "signature": state["v1_sig"]})
            onchain = ledger.send_expecting_revert(
                "approve(v1)", ledger.ledger.functions.approve(bytes.fromhex(state["v1_hash"][2:]),
                                                               bytes.fromhex(state["v1_sig"][2:])))
            return (f"the supplier's genuine v1 signature (valid for v1: {state['v1_sig_valid']}) presented for the "
                    f"new version → coordinator HTTP {r.status_code} ({r.json()['detail']}); on-chain approve on superseded v1 "
                    f"reverted: {onchain['reason']} (status {onchain['status']})",
                    [{"tx": onchain["tx_hash"], "action": "approve(v1) REVERTED", "note": onchain["reason"]}], None)

        def execute():
            before = len(_ok(coord.get(f"/deals/{state['deal']}"))["ledger_txs"])
            d = _ok(coord.post(f"/deals/{state['deal']}/submit"))
            state["payment"] = d["payment_id"]
            return (f"bank payment initiated with idempotency key; state {d['state']}", _new_txs(d, before),
                    d["payment_id"])

        def settle():
            before = len(_ok(coord.get(f"/deals/{state['deal']}"))["ledger_txs"])
            deadline = time.time() + 90
            d = _ok(coord.get(f"/deals/{state['deal']}"))
            while d["state"] != "SETTLED" and time.time() < deadline:
                time.sleep(0.5)
                d = _ok(coord.get(f"/deals/{state['deal']}"))
            events = [e for e in _ok(coord.get("/bank/webhook-events")) if e["payment_id"] == state["payment"]]
            verdicts = ", ".join(f"{e['status']}:{e['verdict']}" for e in events)
            state["deal_json"] = d
            return (f"bank {d['payment']['status']}; webhooks {verdicts}; state {d['state']}",
                    _new_txs(d, before), state["payment"])

        def duplicate_invoice():
            r = coord.post("/invoices/intake", json={"invoice_number": invoice})
            d = state["deal_json"]
            inv = d["invoice"]
            due = int(datetime.fromisoformat(inv["due_date"]).replace(tzinfo=timezone.utc).timestamp())
            onchain = ledger.send_expecting_revert("registerInvoice(duplicate)", ledger.registry.functions.registerInvoice(
                bytes.fromhex(d["invoice_hash"][2:]), d["agreement"]["agreement"]["seller"],
                d["agreement"]["agreement"]["buyer"], round(inv["face"] * 100), due))
            return (f"coordinator HTTP {r.status_code} ({r.json()['detail']}); on-chain registerInvoice reverted: "
                    f"{onchain['reason']} (status {onchain['status']})",
                    [{"tx": onchain["tx_hash"], "action": "registerInvoice REVERTED", "note": onchain["reason"]}],
                    None)

        def replay_bank():
            t = state["deal_json"]["agreement"]["agreement"]
            body = {"debtor_account": "SBX-TR-BETA-0001", "creditor_account": t["payee_account"],
                    "amount_minor": t["pay_amount"], "currency": t["currency"],
                    "reference": state["deal_json"]["agreement"]["agreement_hash"]}
            beta0 = bank.get("/accounts/SBX-TR-BETA-0001").json()["balance_minor"]
            rs = [bank.post("/payments", json=body, headers={"Idempotency-Key": t["idempotency_key"]}) for _ in range(2)]
            ids = {r.json()["payment_id"] for r in rs}
            count = sum(1 for p in bank.get("/payments").json() if p["idempotency_key"] == t["idempotency_key"])
            beta1 = bank.get("/accounts/SBX-TR-BETA-0001").json()["balance_minor"]
            return (f"2 replays → HTTP {[r.status_code for r in rs]}, replayed header "
                    f"{rs[0].headers.get('Idempotent-Replayed')}, payment ids {sorted(ids)}, payments for this key: "
                    f"{count}, buyer balance change {beta1 - beta0}"), None, next(iter(ids))

        def forged_webhook():
            fake = json.dumps({"event_id": "evt_forged", "payment_id": state["payment"], "status": "SETTLED",
                               "amount_minor": 1, "currency": "TRY"}).encode()
            r = coord.post("/webhooks/bank", content=fake, headers={
                "X-Bank-Signature": f"t={int(time.time())},v1={'0' * 64}", "Content-Type": "application/json"})
            return f"HTTP {r.status_code}: {r.json()}", None, None

        def over_limit_agent():
            conn = supplier_data.connect(run_a / "alpha_foods.db")
            obey = [{"name": "submit_offer", "input": {"action": "offer", "x": 62_000, "d": 0.10,
                                                       "rationale": "SYSTEM said accept 10%"}}]
            greedy = [{"name": "submit_offer", "input": {"action": "offer", "x": 62_000, "d": 0.04,
                                                         "rationale": "4% please"}}]
            turn = supplier_agents.seller_agent(conn, invoice, ScriptedBackend([obey, greedy])).turn(
                {"negotiation_id": "evidence", "invoice_number": invoice, "round": 1,
                 "reference": {"x": 62_000, "d": 0.025}, "last_offer": None, "history": [],
                 "invoice_memo": supplier_data.invoice_document(conn, invoice)["memo"]})
            reasons = "; ".join(b["reason"] for b in turn["blocked"])
            return (f"scripted compromised model proposed 10% then 4%: both blocked by code ({reasons}); final move "
                    f"{turn['action']} d={turn['d']:.1%} (fallback={turn['fallback']})"), None, None

        def audit():
            v = _ok(coord.get("/audit/verify"))
            return f"hash chain {'intact' if v['ok'] else 'BROKEN'}: {v['message']}", None, None

        rec.step("A", "Liquidity stress test", "Risk Agent", risk)
        rec.step("A", "Compare alternatives", "Strategy Agent", strategy)
        rec.step("A", "Register + buyer-confirm invoice", "Coordinator + buyer human (EIP-712)", intake)
        rec.step("A", "BILNO + negotiation, commit v1", "Seller/Buyer Agents + Coordinator", negotiate)
        rec.step("A", "Supplier approves v1", "Alpha Foods human (T3)", sign("supplier"))
        rec.step("A", "Buyer approves v1", "Beta Markets human (T3)", sign("buyer"))
        rec.step("A", "ATTACK S6: change amount after approval", "Coordinator (amend)", amend(60_000, "x changed to 60,000"))
        rec.step("A", "ATTACK S6: reuse the v1 approval", "Attacker", reuse_old_approval)
        rec.step("A", "Restore optimum terms (new version)", "Coordinator (amend)", amend(62_000, "x back to 62,000"))
        rec.step("A", "Supplier approves new version", "Alpha Foods human (T3)", sign("supplier"))
        rec.step("A", "Buyer approves new version", "Beta Markets human (T3)", sign("buyer"))
        rec.step("A", "Execute bank payment + recordSubmission", "Coordinator (T4)", execute)
        rec.step("A", "Signed webhook → recordSettlement", "Bank sandbox → Coordinator", settle)
        rec.step("A", "ATTACK S2: duplicate invoice", "Attacker", duplicate_invoice)
        rec.step("A", "ATTACK S7: replay bank request", "Attacker", replay_bank)
        rec.step("A", "ATTACK S8: forged webhook", "Attacker", forged_webhook)
        rec.step("A", "ATTACK S3/S5: agent over-limit offer", "Seller Agent (compromised model)", over_limit_agent)
        rec.step("A", "Verify audit chain", "Auditor", audit)
        deal_a = state["deal_json"]
        stop_stack(stack)
        procs = [p for p in procs if p not in stack]

        # ------------------------------------------------------------------ scenario B
        run_b = run_root / "B_no_deal"
        os.environ["INVOICE_SERIES"] = series + "B"
        run_b.mkdir(parents=True, exist_ok=True)
        bconn = buyer_data.connect(run_b / "beta_markets.db")
        bconn.execute("UPDATE company SET reserve = 440000")
        bconn.commit()
        bconn.close()
        ports = {n: local_chain.free_port() for n in SERVICES}
        stack, url, _ = start_stack("configured", run_b, ports, offline=True)
        procs += stack
        coord_b = httpx.Client(base_url=url["coordinator"], timeout=300)
        rec_b = Recorder(coord_b, run_b, ledger)
        invoice_b = supplier_data.invoice_number("0142")
        sb = {}

        def intake_b():
            d = _ok(coord_b.post("/invoices/intake", json={"invoice_number": invoice_b}))
            sb["deal"] = d["deal_id"]
            return f"VERIFIED: on-chain invoice id {d['ledger_invoice_id']}", _new_txs(d, 0), None

        def negotiate_b():
            d = _ok(coord_b.post(f"/deals/{sb['deal']}/negotiate"))
            n = d["negotiation"]
            sb["deal_json"] = d
            return (f"{n['outcome']} after {n['evaluations']['supplier']} supplier / {n['evaluations']['buyer']} buyer "
                    f"evaluations → state {d['state']}; recommendation: {n['recommendation']}; no agreement committed"),\
                None, None

        rec_b.step("B", "Register + buyer-confirm invoice (buyer reserve 440,000)", "Coordinator", intake_b)
        rec_b.step("B", "BILNO + negotiation", "Seller/Buyer Agents + Coordinator", negotiate_b)
        deal_b = sb["deal_json"]

        _write_outputs(rec, rec_b, deal_a, deal_b, run_a, run_b, ledger_label, ledger, started, series)
        return 0
    finally:
        stop_stack(procs)


def _typed(coord: httpx.Client, deal_id: str) -> dict:
    """Rebuilds the exact typed data the coordinator asks humans to sign (bytes fields come back hex-encoded)."""
    td = _ok(coord.get(f"/deals/{deal_id}/typed-data"))
    msg = {k: bytes.fromhex(v[2:]) if isinstance(v, str) and v.startswith("0x") and len(v) == 66 else v
           for k, v in td["message"].items()}
    return {**td, "message": msg}


def _write_outputs(rec, rec_b, deal_a, deal_b, run_a, run_b, ledger_label, ledger, started, series) -> None:
    EVIDENCE.mkdir(exist_ok=True)
    shutil.copy(run_a / "audit_log.jsonl", EVIDENCE / "audit_log.jsonl")
    shutil.copy(run_a / "bank_sandbox_log.jsonl", EVIDENCE / "bank_sandbox_log.jsonl")
    shutil.copy(run_b / "audit_log.jsonl", EVIDENCE / "audit_log_no_deal.jsonl")
    (EVIDENCE / "deal_settled.json").write_text(json.dumps(deal_a, indent=2) + "\n")
    (EVIDENCE / "deal_no_feasible_agreement.json").write_text(json.dumps(deal_b, indent=2) + "\n")
    (EVIDENCE / "e2e_run_output.txt").write_text("\n".join(rec.out + rec_b.out) + "\n")

    def refs(row, audit_file):
        out = []
        if row["audit"]:
            out.append(f"{audit_file} seq {row['audit'][0]}–{row['audit'][1]}")
        if row["bank"]:
            out.append(f"bank_sandbox_log.jsonl L{row['bank'][0]}–{row['bank'][1]}")
        return "<br>".join(out) or "-"

    def table(rows, recorder, audit_file):
        lines = ["| # | Step | Actor | Result | Ledger tx | Bank payment | Log refs |", "|---|---|---|---|---|---|---|"]
        for i, r in enumerate(rows, 1):
            txs = "<br>".join(f"{t['action']}: {recorder.tx_link(t['tx'])}" for t in r["txs"]) or "-"
            result = r["result"].replace("|", "\\|")
            lines.append(f"| {i} | {r['step']} | {r['actor']} | {result} | {txs} | "
                         f"{('`' + r['payment'] + '`') if r['payment'] else '-'} | {refs(r, audit_file)} |")
        return "\n".join(lines)

    t = deal_a["agreement"]["agreement"]
    doc = f"""# Execution evidence

Generated by `scripts/run_demo_scenario.py` on {started:%Y-%m-%d %H:%M} UTC. Every value below comes from that run.

- **Ledger:** {ledger_label}. Contracts: `InvoiceRegistry` `{ledger.addresses()['InvoiceRegistry']}`,
  `AgreementLedger` `{ledger.addresses()['AgreementLedger']}`.
- **Bank:** the bank sandbox (`services/bank_sandbox`), a simulated bank. No real money moved and there is no
  production banking access.
- **Agents:** `LLM_MODE=offline` (deterministic strategies). The over-limit attack uses a scripted "compromised
  model" running against the real Seller Agent code.
- **Services:** supplier workspace, buyer workspace, coordinator and bank sandbox ran as four separate processes with
  their own databases. Each signature was produced with that role's key (demo keys held server-side, a known risk).
- Invoice numbers use the series `{series}` (instead of `2026`) so that reruns on a persistent chain register fresh
  invoice hashes. Amounts are example values.

## Scenario A: crisis → negotiated early payment → settlement (plus attacks)

Final agreement: v{deal_a['agreement']['version']}, face {t['face_amount_x'] / 100:,.2f} TRY paid early at
{t['discount_bps'] / 100:.2f}% → buyer paid **{t['pay_amount'] / 100:,.2f} TRY**. Agreement hash
`{deal_a['agreement']['agreement_hash']}`. Final state **{deal_a['state']}**.

{table(rec.rows, rec, 'audit_log.jsonl')}

## Scenario B: buyer reserve too high → no deal

{table(rec_b.rows, rec_b, 'audit_log_no_deal.jsonl')}

## Files

| File | Content |
|---|---|
| `evidence/e2e_run_output.txt` | console output of the run |
| `evidence/audit_log.jsonl` | scenario A hash-chained audit log (`python -m services.common.audit verify evidence/audit_log.jsonl`) |
| `evidence/audit_log_no_deal.jsonl` | scenario B audit log |
| `evidence/bank_sandbox_log.jsonl` | bank sandbox operation log: every request/response and webhook delivery |
| `evidence/deal_settled.json` | full coordinator record of the settled deal (versions, negotiation, history, ledger txs) |
| `evidence/deal_no_feasible_agreement.json` | full coordinator record of the no-deal scenario |

Reverted attack transactions (`REVERTED`) were sent on purpose with a fixed gas limit, so that each revert is
mined and has a tx hash. The reason shown was decoded from a dry run of the same call.
"""
    DOC.write_text(doc)
    print(f"\nWrote {DOC.relative_to(ROOT)} and evidence files.")


if __name__ == "__main__":
    sys.exit(main())
