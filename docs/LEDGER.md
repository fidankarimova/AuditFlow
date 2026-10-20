# Ledger target

**Used: EVM — Base Sepolia testnet (`LEDGER_TARGET=evm`, chain id 84532).**

FISCO BCOS 3.x was the preferred target but was **not attempted** in this phase: the team decided to deploy
directly to Base Sepolia to protect the deadline. A FISCO attempt may be made at the end, limited to 3 failed
attempts. Until a FISCO deployment actually works, nothing in this repo claims FISCO BCOS support.

- Contracts: `InvoiceRegistry`, `AgreementLedger` (`contracts/src/`), deployed by `contracts/script/Deploy.s.sol`.
- Addresses + deploy tx hashes: `deployments/base-sepolia.json`.
- Python client: `services/ledger/client.py` — `LedgerClient` interface with `EvmLedgerClient` (implemented) and
  `FiscoLedgerClient` (stub that raises `NotImplementedError`).
- Check: `python -m services.ledger.client ping`.

## Local rehearsal chain and disabled ledger (Phase 7)

- `services/ledger/local_chain.py` starts **anvil** (local chain id 31337) and deploys the same contracts with
  `forge script`, using Anvil's public test keys. Tests and `make flow` use it when Foundry is installed. It is a
  local test chain: not Base Sepolia, not FISCO BCOS.
- `LEDGER_TARGET=none` (`NoLedger`) disables the ledger: every write returns `tx_hash: null` with the note
  "ledger disabled", and the UI and CLI show it that way.
- `EvmLedgerClient` write methods: `register_invoice`, `confirm_invoice_with_sig`, `commit_agreement`, `approve`,
  `supersede`, `record_submission`, `record_settlement`, `record_failure`. Reverts are raised as `LedgerError` with
  the decoded custom error name (e.g. `DuplicateInvoice`, `MissingApprovals`).
- The off-chain agreement hash (`services/common/eip712.py`) equals the on-chain `hashAgreement` (tested on anvil).
