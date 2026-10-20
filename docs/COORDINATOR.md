# Agents, negotiation and the transaction coordinator (Phase 7)

All money math, limits and approvals are deterministic code. The LLM agents only propose, argue and explain.
`LLM_MODE=offline` (the default) runs every agent with a deterministic strategy through the same tool registries.

## Agents and their tools

| Agent | Runs in | Tools (registry) | Code |
|---|---|---|---|
| Risk Agent | supplier workspace | `run_stress_test` | `services/supplier/agents.py` |
| Strategy Agent | supplier workspace | `list_eligible_invoices`, `price_alternatives` | `services/supplier/agents.py` |
| Seller Agent | supplier workspace | `evaluate_counter`, `submit_offer` | `services/common/negotiator.py` |
| Buyer Agent | buyer workspace | `evaluate_counter`, `submit_offer` | `services/common/negotiator.py` |
| Compliance Agent | coordinator | `check_invoice`, `check_agreement` | `services/coordinator/compliance.py` |

- Every agent has its own `ToolRegistry` (`services/common/llm.py`). A call to any other tool raises
  `UnlistedToolError` and is audit-logged as tier **T5**.
- `submit_offer` runs `Negotiator.validate_offer()`: amount/discount grid, public 5% cap, and the company's own
  private `check()` (supplier: max discount, min net amount, gain vs best alternative, liquidity risk limit;
  buyer: max early cash, gain vs paying at the due date, reserve risk limit). Offers that break these are rejected
  by code whatever the model proposed. If a model fails twice, the turn falls back to the deterministic strategy and
  the transcript shows `fallback: true` plus the blocked attempts.
- Untrusted text (invoice memos, the other side's rationale) reaches a model only inside `<untrusted>` tags.
  The system prompt says limits come from code and that private numbers must never be revealed.
- Online mode: Anthropic Messages API, manual tool loop, `tool_choice: auto`, `strict` tool schemas, server-side
  refusal fallback (`fallbacks: "default"`) for models that support it, `LLM_MODEL` (default `claude-opus-5-5`).
  Request shape is tested with a mocked transport; it was not run against the live API in this phase (no key).

## Negotiation (`services/coordinator/negotiation.py`)

1. BILNO computes `(x*, d*)` using only each workspace's `/evaluate` (feasible + 100 TL gain bucket).
2. Up to 3 rounds each way. The seller wants a low discount, the buyer a high one. Offline: the seller goes
   `d*-0.4% → d*-0.2% → d*`, the buyer `d*+0.4% → d*+0.2% → d*`. (The plan text said the seller opens at
   `d*+0.5%`. That favours the buyer, so the seller opens below `d*` instead.)
3. Close only when one side accepts the other's exact offer, both workspaces still call it feasible, and its Nash
   product is within 10% of the BILNO optimum. Otherwise the result is `NO_FEASIBLE_AGREEMENT` plus the best
   alternative (e.g. `bank_loan`).
4. Rate limits (workspace side): 200 evaluations per negotiation, 3 negotiations per invoice.

## Transaction coordinator (`services/coordinator/transactions.py`)

`VERIFIED → NEGOTIATED → APPROVED → SUBMITTED → SETTLED`. Side exits are `REJECTED`, `EXPIRED`, `CANCELLED` and `FAILED`.
Any other transition raises `IllegalTransition` (HTTP 409).

| Step | Rule | Ledger write |
|---|---|---|
| Intake → VERIFIED | Compliance checks + buyer workspace recognises the payable + **buyer's EIP-712 InvoiceConfirmation signature** (the supplier's own "confirmed" flag is not trusted). Duplicate invoice hash → 409. | `registerInvoice`, `confirmByBuyerWithSig` |
| → NEGOTIATED | Negotiation closed + `check_agreement` passes; agreement v1 hashed with EIP-712 | `commitAgreement` |
| → APPROVED | Both humans' signatures recover to the seller/buyer approver addresses over the **current** version's hash | `approve` ×2 |
| amend | Any change → new version, new hash, new idempotency key, signatures cleared, old version superseded | `supersede`, `commitAgreement` |
| → SUBMITTED | Not expired; compliance re-check; bank payment with the agreement's idempotency key (safe retries) | `recordSubmission` |
| → SETTLED | Only after an HMAC-verified bank webhook (`SETTLED`) for that payment id and amount. A poll alone never settles. | `recordSettlement` |
| → FAILED | Bank reports `FAILED` | `recordFailure` |

Human approval (T3): `POST /deals/{id}/sign {role}` signs with the role's demo key held server-side (known risk;
`GET /deals/{id}/typed-data` + `POST /deals/{id}/approve` accept an external signature).

## Audit log (`services/common/audit.py`)

Append-only, hash-chained JSONL with fields `seq, ts, actor, action, tier, inputs_hash, result, prev_hash, entry_hash`.
It records every agent tool call, blocked or denied attempts, human approvals, state changes, ledger writes and
rejected webhooks. Verify it with `make audit-verify`, `python -m services.common.audit verify <path>`, or
`GET /audit/verify`.

## Run it

```bash
make flow     # python -m services.coordinator.run_flow --offline
```

This starts all four services as separate processes (and a local anvil chain if Foundry is installed). It runs
detection → strategy → verification → negotiation → approvals → bank → webhook → settlement → ledger, and prints the
state history and the ledger tx hashes. Run artifacts go to `data/runs/<ts>/`.

## Tests (`tests/test_phase7.py`)

`test_S1_forged_invoice_not_verified`, `test_S2_duplicate_invoice_rejected` (off-chain + on-chain),
`test_S3_prompt_injection_in_memo_ignored`, `test_S4_buyer_agent_over_limit_blocked`,
`test_S5_seller_agent_over_discount_blocked`, `test_S6_changed_amount_invalidates_approval`,
`test_S10_limit_probing_rate_limited`, `test_agent_cannot_call_unlisted_tool`,
`test_settlement_requires_both_approvals`, `test_audit_chain_tamper_detected`, plus the happy path, no-deal
(buyer reserve 440,000 → bank loan), illegal transitions, expiry, polling-never-settles, two on-chain tests
(anvil), and the online request shape. S7–S9 are in `tests/test_phase6.py`.

## Known risks introduced here (for the security self-assessment)

- In online mode the other side reads the free-text `rationale` a model writes. The prompt forbids revealing
  private numbers, but no code filter enforces this yet. Offline rationales are fixed strings.
- The transcript shows which `(x, d)` points each agent tested with `evaluate_counter`, but not the results.
- Human approver keys are demo keys held by the coordinator. If unset, they are PUBLIC anvil test keys.
- The webhook-triggered settlement runs in a background thread. If the coordinator stops between the webhook and
  the ledger write, `refresh-payment` re-runs the step (`_advance_payment` is idempotent).
