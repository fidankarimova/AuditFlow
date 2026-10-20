"""Runs the whole demo stack on fixed ports until Ctrl-C: supplier 8101, buyer 8102, coordinator 8200, bank 8300.

  python scripts/stack.py                     # local anvil chain if Foundry is installed, else ledger disabled
  python scripts/stack.py --ledger configured # use LEDGER_TARGET / RPC_URL / keys from .env (e.g. Base Sepolia)

Fresh databases and logs per run under data/runs/<ts>/.
"""

import argparse
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from services.coordinator.run_flow import start_stack, stop_stack  # noqa: E402

def _on_sigterm(*_):
    raise KeyboardInterrupt


PORTS = {"supplier": 8101, "buyer": 8102, "coordinator": 8200, "bank": 8300}

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ledger", choices=["auto", "anvil", "none", "configured"], default="auto")
    ap.add_argument("--online", action="store_true", help="use LLM_MODE from .env instead of forcing offline")
    args = ap.parse_args()
    signal.signal(signal.SIGTERM, _on_sigterm)  # `kill` stops the child services too, like Ctrl-C
    run_dir = ROOT / "data" / "runs" / f"stack-{datetime.now():%Y%m%d-%H%M%S}"
    if args.ledger == "configured" and "INVOICE_SERIES" not in os.environ:
        # a persistent chain remembers invoice hashes: give every restart fresh invoice numbers
        os.environ["INVOICE_SERIES"] = f"D{datetime.now():%m%d%H%M%S}"
    procs, url, env = start_stack(args.ledger, run_dir, PORTS, offline=not args.online)
    print(f"Ledger: {env.get('LEDGER_TARGET', 'from .env')}  LLM_MODE: {env.get('LLM_MODE', 'from .env')}")
    for name, u in url.items():
        print(f"  {name:<12} {u}")
    print(f"Logs and DBs: {run_dir}\nRun `make ui` in another terminal. Ctrl-C to stop.")
    try:
        while all(p.poll() is None for p in procs):
            time.sleep(1)
        print("A service exited; see the logs above.")
    except KeyboardInterrupt:
        pass
    finally:
        stop_stack(procs)
