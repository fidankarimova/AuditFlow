SHELL := /bin/bash
FORGE ?= $(shell command -v forge 2>/dev/null || echo $(HOME)/.foundry/bin/forge)
PYTHON ?= .venv/bin/python

.PHONY: install build test test-contracts test-py coverage deploy deploy-local ping fisco-up fisco-stop deploy-fisco ping-fisco up-fisco demo-fisco up bank coordinator supplier buyer flow audit-verify demo ui

# First Python >= 3.11 on PATH (override with `make install PY=/path/to/python3.12`)
PY ?= $(shell for p in python3.13 python3.12 python3.11 python3; do command -v $$p >/dev/null 2>&1 && $$p -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && echo $$p && break; done)

install:
	@test -n "$(PY)" || (echo "Python 3.11+ not found; install it or pass PY=/path/to/python3.11" && exit 1)
	$(PY) -m venv .venv
	.venv/bin/pip install -e .

build:
	cd contracts && $(FORGE) build

test: test-contracts test-py

test-contracts:
	cd contracts && $(FORGE) test -vv

test-py:
	$(PYTHON) -m pytest -q

coverage:
	cd contracts && $(FORGE) coverage --no-match-coverage "(test|script)/" | grep -E "^\|" > ../evidence/forge-coverage.txt
	cat evidence/forge-coverage.txt

# Base Sepolia. Only the admin address needs faucet ETH.
deploy:
	set -a && source .env && set +a && cd contracts && $(FORGE) script script/Deploy.s.sol --rpc-url "$$RPC_URL" --broadcast --slow
	set -a && source .env && set +a && $(PYTHON) scripts/export_deployment.py "$$CHAIN_ID" base-sepolia

# Local rehearsal on anvil (uses anvil's funded default key as admin)
deploy-local:
	set -a && source .env && set +a && cd contracts && ADMIN_PRIVATE_KEY=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80 $(FORGE) script script/Deploy.s.sol --rpc-url http://127.0.0.1:8545 --broadcast
	$(PYTHON) scripts/export_deployment.py 31337 anvil

ping:
	$(PYTHON) -m services.ledger.client ping

# FISCO BCOS 3.x: local 4-node PBFT chain, Web3 RPC on node0 (127.0.0.1:8545, chain id 20200). No gas fees.
FISCO_ENV := LEDGER_TARGET=fisco RPC_URL=http://127.0.0.1:8545 CHAIN_ID=20200
fisco-up:
	scripts/fisco_chain.sh up

fisco-stop:
	scripts/fisco_chain.sh stop

deploy-fisco:
	set -a && source .env && set +a && cd contracts && FOUNDRY_PROFILE=fisco FUND_GAS=false $(FORGE) script script/Deploy.s.sol --rpc-url http://127.0.0.1:8545 --broadcast --legacy --with-gas-price 0 --slow
	$(PYTHON) scripts/export_deployment.py 20200 fisco-local

ping-fisco:
	$(FISCO_ENV) $(PYTHON) -m services.ledger.client ping

up-fisco:
	$(FISCO_ENV) $(PYTHON) scripts/stack.py --ledger configured

# End-to-end evidence on FISCO BCOS -> docs/EXECUTION_EVIDENCE_FISCO.md + evidence/fisco/
demo-fisco:
	$(FISCO_ENV) $(PYTHON) scripts/run_demo_scenario.py --ledger configured --tag fisco

UVICORN ?= .venv/bin/uvicorn

supplier:
	$(UVICORN) services.supplier.app:app --port 8101

buyer:
	$(UVICORN) services.buyer.app:app --port 8102

coordinator:
	$(UVICORN) services.coordinator.app:create_app --factory --port 8200

bank:
	$(UVICORN) services.bank_sandbox.app:create_app --factory --port 8300

# All four services on fixed ports (+ local anvil chain if Foundry is installed). Ctrl-C stops them.
# LEDGER=configured uses LEDGER_TARGET/RPC_URL/keys from .env (e.g. Base Sepolia).
LEDGER ?= auto
up:
	$(PYTHON) scripts/stack.py --ledger $(LEDGER)

# Full flow over real services (local anvil chain if Foundry is installed, otherwise ledger disabled)
flow:
	$(PYTHON) -m services.coordinator.run_flow --offline

audit-verify:
	$(PYTHON) -m services.common.audit verify

# End-to-end run + execution evidence (docs/EXECUTION_EVIDENCE.md, evidence/). LEDGER=configured for Base Sepolia.
demo:
	$(PYTHON) scripts/run_demo_scenario.py --ledger $(if $(filter configured,$(LEDGER)),configured,anvil)

ui:
	$(PYTHON) -m streamlit run ui/Home.py --server.headless true
