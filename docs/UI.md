# UI (Phase 8)

A Streamlit multipage app (`ui/`). It is a thin client over the services' HTTP APIs and contains no business logic.

```bash
make up    # terminal 1: supplier 8101, buyer 8102, coordinator 8200, bank 8300 (+ local anvil chain if Foundry is installed)
make ui    # terminal 2: http://localhost:8501
```

`make up LEDGER=configured` uses `LEDGER_TARGET` / `RPC_URL` / keys from `.env` (e.g. Base Sepolia) instead of a
local chain. Each `make up` starts fresh databases under `data/runs/stack-<ts>/`.

The sidebar has the **role switcher** (Supplier / Buyer / Auditor) and the deal selector. The role switcher is a demo
convenience and not authentication: it changes what the UI shows and which signature button is enabled.

| # | Screen | What it shows | Screenshot |
|---|---|---|---|
| 1 | Financial Command Center | Balance, obligations, receivables, "Cash crisis in N days" alert, recommended action, alternatives. The buyer role sees its own private view. | `evidence/screenshots/1_financial_command_center.png` |
| 2 | Liquidity Stress Lab | p5/p50/p95 fan chart with the reserve line, `p_breach` per scenario, table view, and **Before vs After action** | `2_liquidity_stress_lab.png` |
| 3 | Intelligent Receivables | Eligible invoices with reasons; excluded ones (disputed, unconfirmed) with the reason; memos shown as untrusted text | `3_intelligent_receivables.png` |
| 4 | Live AI Negotiation Room | Verify invoice, **Run negotiation**, BILNO optimum, round-by-round offers, mode, blocked attempts, tool calls, close check or `NO_FEASIBLE_AGREEMENT` | `4_negotiation_room.png` |
| 5 | Privacy & Compliance Verification | What each side revealed, evaluation counters, every optimizer question with its yes/no answer, Compliance Agent checks. Labelled *"Confidential policy verification (server-side). Not a zero-knowledge proof."* | `5_privacy_and_compliance.png` |
| 6 | Agreement & Payment Approval | Exact terms + `agreement_hash`, **Sign** per side (EIP-712), versions, amend (new version), **Execute bank payment** | `6a_agreement_approved.png`, `6b_agreement_executed.png` |
| 7 | Settlement & Audit Explorer | State timeline, ledger txs (explorer links on Base Sepolia), bank payment + status history, webhooks (accepted/rejected), **Replay bank request** (S7) and **Send forged webhook** (S8) attacks, **Verify audit chain** | `7_settlement_and_audit.png` |

The screenshots were taken by clicking through the happy path end to end with a headless browser (local anvil chain,
offline agents). `tests/test_phase8_ui.py` checks that every page renders without errors when the services are down.
