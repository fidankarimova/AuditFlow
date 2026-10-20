# Security self-assessment

**Scope.** AuditFlow AI as built in this repository: a regulation-aware architecture with simulated fiat
execution and no production banking access. This is a self-assessment by the team, not an external audit. Nothing
here claims regulatory compliance.

## 1. Assets and trust boundaries

| Asset | Where it lives | Who may touch it |
|---|---|---|
| Supplier private data: cash, reserve, max discount, min net amount, bank terms | `alpha_foods.db`, supplier workspace only | Supplier workspace code |
| Buyer private data: cash, reserve, opportunity cost, max early cash, risk limit | `beta_markets.db`, buyer workspace only | Buyer workspace code |
| Agreement terms and approvals | coordinator DB + `AgreementLedger` | Coordinator (deterministic code) + each company's human signer |
| Money movement | bank sandbox (simulated) | Coordinator, only after both approvals |
| Audit trail | `evidence/audit_log.jsonl` (hash chain) + ledger events | Append-only |

The trust boundaries are:
- between the two company workspaces (they communicate only through the coordinator's HTTP calls);
- between the agents (LLM) and everything that moves money (code);
- between the coordinator and the bank (HMAC webhooks);
- between the coordinator and the ledger (contract rules the coordinator cannot bypass).

## 2. Threat model

| Threat actor | Goal | Main controls | Tests |
|---|---|---|---|
| Dishonest supplier | Get paid early on a forged or duplicate invoice | Buyer's EIP-712 invoice confirmation; one-time invoice hash off-chain and on-chain; compliance checks (disputed/settled, payee account) | S1, S2 |
| Prompt injection (invoice memo, counter-party text) | Make an agent accept bad terms | `<untrusted>` wrapping, system rules, and **code-level `validate_offer`** on every offer | S3 |
| Compromised or wrong LLM output | Offer/accept outside a company's limits; call privileged tools | Per-agent tool registry (no approve/pay tools exist), `validate_offer`, deterministic fallback | S4, S5, unlisted tool |
| Curious counter-party | Learn the other side's limits by probing | Only `feasible` + 100 TL bucket revealed; 200 evaluations per negotiation; 3 negotiations per invoice | S10 |
| Anyone changing terms after approval | Pay a different amount or payee than was signed | EIP-712 hash over all fields; new version → new hash; on-chain supersede | S6 |
| Network attacker / bank flakiness | Double payment, forged settlement | Idempotency-Key; same key on retries; HMAC-signed, timestamped webhooks; id + amount match | S7, S8, S9 |
| Insider tampering with logs | Rewrite history | Hash-chained audit log + ledger events independent of the coordinator DB | audit tamper test |
| Coordinator bug / operator | Execute without approvals | State machine + **on-chain** `recordSubmission` reverts without both approvals | both approvals test, Foundry tests |

## 3. Permission tiers: enforcement and proof

| Tier | Action | Who | Enforced in (code) | Proven by (test) |
|---|---|---|---|---|
| T0 Read | Own company data, forecasts | Own agents | Separate DBs and services; `services/supplier/data.py`, `services/buyer/data.py` | `test_isolation_supplier_never_imports_buyer_and_vice_versa`, `test_isolation_bank_and_coordinator_never_import_company_workspaces`, `test_evaluate_reveals_no_private_numbers` |
| T1 Advise | Alerts, recommendations, drafts | Risk / Strategy / Compliance agents | `ToolRegistry` (`services/common/llm.py`); agents have no state-changing tools | `test_agent_cannot_call_unlisted_tool` |
| T2 Negotiate | Send / evaluate offers within private limits | Seller / Buyer agents | `Negotiator.validate_offer` (`services/common/negotiator.py`) → `SupplierPolicy.check`, `buyer.policy.check`; BILNO close check (`services/coordinator/negotiation.py`) | `test_S3_*`, `test_S4_*`, `test_S5_*`, `test_supplier_rejects_discount_above_private_limit`, `test_buyer_evaluate_rejects_over_limit_and_unprofitable` |
| T3 Approve | Sign the exact `agreement_hash` | Authorized human of each company | `Coordinator.approve` (EIP-712 recover vs approver address, current version only); `AgreementLedger.approve` | `test_S6_changed_amount_invalidates_approval`, `test_settlement_requires_both_approvals`, `test_onchain_superseded_version_cannot_be_approved`, Foundry `test_RevertWhen_ApprovalWithWrongSigner` / `ApprovalOnSupersededVersion` / `ApprovalReplayed` |
| T4 Execute | Initiate bank payment, record settlement | Coordinator, only after T3 from both sides | `Coordinator.submit` / `_advance_payment` state machine; bank idempotency; webhook verifier (`services/coordinator/payments.py`); `AgreementLedger.recordSubmission` / `recordSettlement` | `test_illegal_transitions_raise`, `test_S7_*`, `test_S8_*`, `test_S9_*`, `test_polling_alone_never_settles`, Foundry `test_RevertWhen_SubmissionWithOneApproval`, `SettlementBeforeSubmission`, `NonCoordinatorCalls` |
| T5 Forbidden | Change limits/policies, read the other side's data, pay without approvals | Nobody automatic | No such endpoint or tool exists; registry denies and logs T5; contract roles; agents hold no key | `test_agent_cannot_call_unlisted_tool`, isolation tests, Foundry `test_RevertWhen_NonAdminPausesOrGrantsRoles` |

## 4. Security test results (S1–S10)

These results are from `pytest -q -k "S1 or S2 or S3 or S4 or S5 or S6 or S7 or S8 or S9 or S10 or unlisted or
both_approvals or tamper"`: **13 passed, 0 failed** (the 13 tests in the table). The full suite is in
`evidence/test_results.txt`. The S2, S3/S5, S4, S6, S7 and S8 attacks were also executed against the running services
on Base Sepolia (`docs/EXECUTION_EVIDENCE.md`, with tx hashes, Basescan links and log line numbers) and on FISCO BCOS
(`docs/EXECUTION_EVIDENCE_FISCO.md`).

| ID | Test | What it shows | Result |
|---|---|---|---|
| S1 | `test_S1_forged_invoice_not_verified` | Unconfirmed / non-onboarded-buyer invoice, disputed invoice, and an invoice "confirmed" with the wrong key are all refused; the buyer workspace rejects an invoice it does not owe | PASS |
| S2 | `test_S2_duplicate_invoice_rejected` | Second intake of the same invoice → HTTP 409; on-chain `registerInvoice` with the same hash reverts `DuplicateInvoice` (local anvil) | PASS |
| S3 | `test_S3_prompt_injection_in_memo_ignored` | Memo reaches the model only as `<untrusted>`; a model that obeys it (10% discount) is blocked twice by code; final offer ≤ `d_max_S` (3%) | PASS |
| S4 | `test_S4_buyer_agent_over_limit_blocked` | Buyer model offers/accepts terms above `max_early_cash` → blocked (`max_early_cash`). In the Base Sepolia run, a scripted buyer model accepting x=100,000 against a 440,000 reserve is blocked by `reserve_risk_limit` (off-chain) | PASS |
| S5 | `test_S5_seller_agent_over_discount_blocked` | Seller model offers 4% > `d_max_S` → blocked (`max_discount`) | PASS |
| S6 | `test_S6_changed_amount_invalidates_approval` | Amend after approval → v2 with new hash and new idempotency key; the genuine v1 signature is rejected for v2; payment impossible | PASS |
| S7 | `test_S7_duplicate_bank_request_single_payment` | Same Idempotency-Key ×3 → same payment, one debit; same key + different body → 409 | PASS |
| S8 | `test_S8_forged_webhook_rejected` | Missing/wrong-key/tampered/stale signatures → 401; unknown id → 404; wrong amount → 422; only the genuine event settles; duplicate redelivery ignored | PASS |
| S9 | `test_S9_bank_outage_safe_retry` | 503 *after* processing (lost response) ×2 → retries with the same key → one payment, one debit; full outage → nothing created, later retry pays once | PASS |
| S10 | `test_S10_limit_probing_rate_limited` | 201st evaluation → 429; 4th negotiation on one invoice → 429; a negotiation that hits the limit ends without a deal | PASS |
| - | `test_agent_cannot_call_unlisted_tool` | Unlisted tool → `UnlistedToolError` + audit entry tier T5; a model asking for `approve_agreement` gets an error | PASS |
| - | `test_settlement_requires_both_approvals` | One signature → no submit, no bank payment; a supplier signature cannot count as the buyer's | PASS |
| - | `test_audit_chain_tamper_detected` | Editing or deleting one audit line is detected at that line | PASS |

Further related tests (also passing): `test_coordinator_fails_closed_without_secret`,
`test_bank_refuses_to_start_without_webhook_secret`, `test_polling_alone_never_settles`,
`test_expired_agreement_cannot_be_approved`, `test_illegal_transitions_raise`, and 39 Foundry tests.

## 5. Known risks

Likelihood and impact are the team's estimates for a pilot deployment of this design.

| # | Risk | Likelihood | Impact | Current mitigation | Production mitigation |
|---|---|---|---|---|---|
| 1 | The confidential policy service is **trusted server-side code, not a ZKP**. A workspace could lie in `evaluate`, and the coordinator cannot verify the answer | Medium | High | Each side only hurts itself by lying about its own limits; final terms are re-checked by both workspaces and signed by both humans | ZK range proofs over committed balances, or TEEs (remote attestation) for the policy service |
| 2 | `gain_bucket` (100 TL) leaks coarse information about the other side's gain | High | Low–Medium | Bucket rounding. The negotiating agents never receive the other side's bucket, but the coordinator's deal record (BILNO result, Nash product) does contain both buckets, and the demo UI shows it to every role | Restrict the BILNO buckets to the coordinator/auditor view; coarser buckets or a feasibility-only interface; differential-privacy noise |
| 3 | Repeated negotiations could still let a side estimate the other side's limits | Medium | Medium | **Rate limits only**: 200 evaluations per negotiation, 3 negotiations per invoice | Per-counterparty budgets over time, anomaly detection on probing patterns, legal terms |
| 4 | Balances and forecasts are **self-reported** by each company | High | High | Shown as self-reported; the stress test is conservative (reserve + α) | Signed bank-account attestations (open banking APIs), ERP integration, audits |
| 5 | **Demo signing keys are held by the server**; if unset, public anvil test keys are used | High (demo) | Critical (prod) | Documented; public keys only for local chains; signing endpoint is explicit per role | Keys never leave each company: hardware wallets / HSM / passkeys sign `typed-data` in the client (`POST /deals/{id}/approve` already accepts external signatures) |
| 6 | **Single admin / coordinator key** (contract admin, COORDINATOR_ROLE) | Medium | High | AccessControl roles; agents have no key; `pause()`; on-chain approval checks bound what the coordinator can do | Multisig admin, HSM-held coordinator key, timelocks, key rotation |
| 7 | **Bank sandbox is simulated**: no PSD2/licensed provider, no production banking access | Certain | n/a for demo | Clearly labelled everywhere; same idempotency/HMAC patterns as real payment APIs | Licensed PSP / bank API; reconciliation; regulatory review (KYC/AML, payment licensing) |
| 8 | **Partial early payment** needs ERP and credit-note/tax handling that is not built | High | Medium | Supported in the optimizer only; documented | ERP integration, credit notes, tax treatment per jurisdiction |
| 9 | The ledger is a **public testnet (Base Sepolia) or a local FISCO BCOS chain whose 4 nodes all run on one machine**, not a real consortium | Certain | Medium | Every document states the target; the FISCO evidence says it is local | Permissioned consortium chain (e.g. FISCO BCOS) with independent nodes for supplier, buyer, bank and auditor |
| 10 | **LLM provider can be down** or refuse | Medium | Low | `LLM_MODE=offline` deterministic agents; online turns fall back to the deterministic strategy; refusal fallback | Multi-provider routing, caching, SLAs |
| 11 | Online-mode rationales could leak private numbers to the other side (prompt-level rule only) | Medium | Medium | System rule + rationale length cap; offline rationales are fixed strings | Code-level filter or structured rationale fields; redaction of numbers |
| 12 | Services have **no authentication** between them; the UI role switcher is not auth; buyer `/internal/*` views are reachable on its port | High (demo) | High (prod) | Local-only demo; isolation is enforced in code structure | mTLS between services, OAuth/OIDC for users, network policies |
| 13 | BILNO sits **at the edge of the 5% risk limit**; on a fresh draw many deals land at 5–7.5% (`docs/EXPERIMENTS.md`) | High | Medium | Reported openly | Optimize against a safety margin (e.g. α − 1 pp) and more paths |
| 14 | Settlement ledger write could fail after money moved | Low | Medium | If the contract reverts, the deal is still marked SETTLED off-chain, with a "LEDGER WRITE FAILED" note in the history and an audit entry. If the RPC is unreachable, the deal stays SUBMITTED; `refresh-payment` retries, and the retry settles it again because the verified webhook is stored | Retry queue for ledger writes; reconciliation job |

Wording rule used throughout: **"regulation-aware architecture, simulated fiat execution, no production banking
access."** We do not claim compliance with any regulation.
