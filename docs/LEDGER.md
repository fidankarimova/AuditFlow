# Ledger targets

The same Solidity contracts (`InvoiceRegistry`, `AgreementLedger`, `contracts/src/`) run on two real targets,
selected by `LEDGER_TARGET`:

| Target | `LEDGER_TARGET` | Chain id | Deployment | Execution evidence |
|---|---|---|---|---|
| **Base Sepolia** (public EVM testnet) | `evm` | 84532 | `deployments/base-sepolia.json` | `docs/EXECUTION_EVIDENCE.md` (Basescan links) |
| **FISCO BCOS 3.17.1** (local 4-node PBFT chain) | `fisco` | 20200 | `deployments/fisco-local.json` | `docs/EXECUTION_EVIDENCE_FISCO.md`, `evidence/fisco/` |
| anvil (local rehearsal, tests) | `evm` | 31337 | created per run | - |
| disabled | `none` | - | - | writes return `tx_hash: null` with the note "ledger disabled" |

Python client: `services/ledger/client.py`: `LedgerClient` interface; `EvmLedgerClient`; `FiscoLedgerClient`
(subclass of the EVM client, see below); `NoLedger`. Check: `python -m services.ledger.client ping`.

## FISCO BCOS

**What runs.** FISCO BCOS v3.17.1 (official release binary), built with the official `build_chain.sh` as an Air
chain of **4 nodes with PBFT consensus**. Node0 exposes FISCO's built-in **Web3-compatible JSON-RPC**
(`[web3_rpc]` in `config.ini`, `127.0.0.1:8545`, web3 chain id 20200). The other nodes take part in consensus only.
After the evidence run, all 4 nodes reported the same latest block (`scripts/fisco_chain.sh status`).

```bash
make fisco-up        # download v3.17.1 (first run), build 4 nodes into ~/fisco, enable Web3 RPC on node0, start
make deploy-fisco    # forge script with the `fisco` profile (evm_version paris), legacy txs, gas price 0
make ping-fisco
make demo-fisco      # end-to-end run with attacks -> docs/EXECUTION_EVIDENCE_FISCO.md + evidence/fisco/
make up-fisco        # services on FISCO (then `make ui`)
make fisco-stop
```

**Differences from Ethereum that we had to handle (all in code, all tested):**

- **`block.timestamp` is in milliseconds** on FISCO BCOS. The first run failed with `InvalidInvoice` because a due
  date in seconds looked like it was in the past. `contracts/src/ChainTime.sol` returns seconds on both kinds of
  chain (a value above 1e11 is treated as milliseconds). Test: `test_MillisecondTimestampsLikeFiscoBcos`.
- **No gas fees.** `FiscoLedgerClient` sends legacy transactions with `gasPrice = 0`, and the deploy script skips
  forwarding gas money (`FUND_GAS=false`).
- **Older EVM.** Contracts are compiled for `paris` (no `PUSH0`/`MCOPY`) in the `fisco` Foundry profile.

**Honest limits.**

- All 4 nodes run on **one machine** and are operated by us. This shows the contracts and the flow on FISCO BCOS. It
  is **not** a multi-organization consortium in which supplier, buyer, bank and auditor each run a node.
- We use FISCO's Web3 RPC, not its native SDK (`fisco-bcos-python-sdk` / Java SDK), FISCO groups, or its
  permission (auth) contracts.
- EIP-712 signatures and transactions use secp256k1 keys. FISCO's SM (national crypto) mode is not used.

## Base Sepolia

- Deployed by `contracts/script/Deploy.s.sol`. Only the admin needed faucet ETH; it forwards gas to the coordinator
  and supplier. Buyer confirmations and human approvals are EIP-712 signatures relayed by the coordinator.
- The Base Sepolia contracts were deployed **before** `ChainTime.sol` was added for FISCO. On chains with timestamps
  in seconds, `ChainTime.nowSeconds()` returns `block.timestamp` unchanged, so the behaviour is the same. The
  deployed bytecode, however, comes from the earlier source (commit `c96f09d`).
- Public RPCs are load-balanced. The client retries a reverted gas estimate a few times (a node may not yet have
  seen our previous transaction) and never reuses a nonce.

## Local rehearsal chain

- `services/ledger/local_chain.py` starts **anvil** (chain id 31337) and deploys the same contracts with
  `forge script`, using Anvil's public test keys. Tests and `make flow` use it when Foundry is installed.
- `EvmLedgerClient` write methods: `register_invoice`, `confirm_invoice_with_sig`, `commit_agreement`, `approve`,
  `supersede`, `record_submission`, `record_settlement`, `record_failure`. Reverts are raised as `LedgerError` with
  the decoded custom error name (e.g. `DuplicateInvoice`, `MissingApprovals`).
- The off-chain agreement hash (`services/common/eip712.py`) equals the on-chain `hashAgreement` (tested on anvil).
