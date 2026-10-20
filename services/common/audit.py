"""Append-only, hash-chained audit log (JSONL).

Each entry: seq, ts, actor, action, tier, inputs_hash, result, prev_hash, entry_hash, where
entry_hash = sha256(canonical JSON of every other field). Changing, removing or reordering any line breaks
the chain from that point, and `verify` reports the first bad line.

CLI: python -m services.common.audit verify [path]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from services.common.config import REPO_ROOT

DEFAULT_PATH = REPO_ROOT / "evidence" / "audit_log.jsonl"
GENESIS = "0" * 64
TIERS = ("T0", "T1", "T2", "T3", "T4", "T5")


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: Path | None = None):
        self.path = Path(path or os.environ.get("AUDIT_LOG", DEFAULT_PATH))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _last(self) -> tuple[int, str]:
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        e = json.loads(last)
        return e["seq"], e["entry_hash"]

    def append(self, actor: str, action: str, tier: str, inputs=None, result=None) -> dict:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier}")
        with self.lock:
            seq, prev = self._last()
            entry = {
                "seq": seq + 1,
                "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                "actor": actor,
                "action": action,
                "tier": tier,
                "inputs_hash": sha256_hex(inputs),
                "result": result,
                "prev_hash": prev,
            }
            entry["entry_hash"] = sha256_hex(entry)
            with self.path.open("a") as f:
                f.write(canonical(entry) + "\n")
            return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def verify(path: Path) -> tuple[bool, int, str]:
    """Returns (ok, entries checked, message). Stops at the first broken entry."""
    prev, n = GENESIS, 0
    if not Path(path).exists():
        return True, 0, "empty log"
    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "entry_hash"}
        except (ValueError, AttributeError):
            return False, n, f"line {lineno}: not a JSON object"
        if e.get("seq") != n + 1:
            return False, n, f"line {lineno}: seq {e.get('seq')} != expected {n + 1}"
        if e.get("prev_hash") != prev:
            return False, n, f"line {lineno}: prev_hash does not match the previous entry"
        if sha256_hex(body) != e.get("entry_hash"):
            return False, n, f"line {lineno}: entry_hash mismatch (entry was modified)"
        prev, n = e["entry_hash"], n + 1
    return True, n, f"{n} entries, chain intact"


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "verify":
        print("usage: python -m services.common.audit verify [path]")
        return 2
    path = Path(argv[1]) if len(argv) > 1 else Path(os.environ.get("AUDIT_LOG", DEFAULT_PATH))
    ok, n, msg = verify(path)
    print(f"{'OK' if ok else 'TAMPERED'}: {path}: {msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
