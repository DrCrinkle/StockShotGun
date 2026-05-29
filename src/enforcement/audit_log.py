from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from enforcement.errors import AuditChainBroken

GENESIS_HASH = "0" * 64


@dataclass
class AuditEntry:
    ts: str
    kind: str
    intent_hash: str | None = None
    token: str | None = None
    broker: str | None = None
    account_id: str | None = None
    dry_run: bool = True
    result: str = "ok"
    reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = GENESIS_HASH
    this_hash: str = ""


def _compute_hash(entry_no_hash: dict[str, Any]) -> str:
    payload = json.dumps(entry_no_hash, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class AuditLog:
    """Tamper-evident JSONL log. Every line carries `prev_hash` referencing the
    previous line's `this_hash`. `verify()` walks the chain and reports breaks.

    Append-only. Thread-safe (lock around append + tail-read of prev_hash).
    Never log credentials; callers are responsible for redaction before write.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_tail_hash(self) -> str:
        if not self.path.exists():
            return GENESIS_HASH
        last = GENESIS_HASH
        with self.path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    last = rec.get("this_hash", last)
                except json.JSONDecodeError:
                    continue
        return last

    def append(self, entry: AuditEntry) -> AuditEntry:
        with self._lock:
            entry.ts = datetime.now(UTC).isoformat()
            entry.prev_hash = self._read_tail_hash()
            entry.this_hash = ""
            no_hash = asdict(entry)
            no_hash.pop("this_hash")
            entry.this_hash = _compute_hash(no_hash)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
            return entry

    def verify(self) -> tuple[bool, int, str | None]:
        """Return (ok, lines_checked, first_break_message_or_None)."""
        if not self.path.exists():
            return True, 0, None
        prev = GENESIS_HASH
        count = 0
        with self.path.open("r", encoding="utf-8") as f:
            for lineno, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                count += 1
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError as e:
                    return False, count, f"line {lineno}: invalid JSON ({e})"
                if rec.get("prev_hash") != prev:
                    return False, count, f"line {lineno}: prev_hash mismatch"
                claimed = rec.get("this_hash", "")
                no_hash = {k: v for k, v in rec.items() if k != "this_hash"}
                expected = _compute_hash(no_hash)
                if claimed != expected:
                    return False, count, f"line {lineno}: this_hash mismatch"
                prev = claimed
        return True, count, None

    def require_intact(self) -> None:
        ok, _, msg = self.verify()
        if not ok:
            raise AuditChainBroken(f"audit log tamper detected: {msg}")

    def sum_executed_usd_today(self) -> float:
        """Sum `extra.usd_amount` for execute entries with dry_run=False on the
        current UTC date. Used by the per-day limit gate.
        """
        if not self.path.exists():
            return 0.0
        today = datetime.now(UTC).date().isoformat()
        total = 0.0
        with self.path.open("r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if rec.get("kind") != "execute":
                    continue
                if rec.get("dry_run"):
                    continue
                if rec.get("result") != "ok":
                    continue
                if not str(rec.get("ts", "")).startswith(today):
                    continue
                amt = rec.get("extra", {}).get("usd_amount")
                if isinstance(amt, (int, float)):
                    total += float(amt)
        return total
