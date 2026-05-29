from __future__ import annotations

import hashlib
import json

from enforcement.types import OrderIntent


def intent_hash(intent: OrderIntent) -> str:
    """SHA-256 of normalized intent. Stable across runs.

    Token-binding works by storing this hash with the proposal and re-hashing
    the execute() intent; mismatch means the agent tried to substitute a
    different order than the principal approved.
    """
    n = intent.normalized()
    payload = json.dumps(
        {
            "ticker": n.ticker,
            "side": n.side.value,
            "qty": n.qty,
            "targets": [list(t.as_tuple()) for t in n.targets],
            "price": n.price,
            "dry_run": n.dry_run,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def idempotency_key(token: str, broker: str, account_id: str) -> str:
    """Deterministic per-broker-leg key. Broker MCPs MUST refuse to re-place
    an order with the same idempotency_key against the same broker+account,
    so network retries on execute() do not double-fill.
    """
    payload = f"{token}|{broker}|{account_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
