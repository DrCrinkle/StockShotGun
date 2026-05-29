"""Shared safety enforcement library for StockShotGun.

Every order-placing code path — CLI, TUI, router MCP, per-broker MCPs —
imports this module and calls `gate_order(...)` before any live broker call.
This is the single source of truth for: dollar limits, dry-run defaults,
intent-binding confirmation tokens, idempotency keys, reconciliation, the
corporate-action freeze list, circuit breakers, settled-cash/PDT checks,
and the tamper-evident audit log.

Code enforces safety. Prompts do not.
"""

from __future__ import annotations

from enforcement.audit_log import AuditEntry, AuditLog
from enforcement.errors import (
    AuditChainBroken,
    CircuitOpen,
    DailyLimitExceeded,
    FrozenTicker,
    GateError,
    IntentMismatch,
    LiveOrderRequiresConfirmation,
    PerOrderLimitExceeded,
    ReconciliationDivergence,
    TokenAlreadyUsed,
    TokenExpired,
    TokenInvalid,
)
from enforcement.gate import EnforcementCore, gate_order
from enforcement.intent import idempotency_key, intent_hash
from enforcement.propose_execute import (
    ProposalStore,
    propose_fanout,
    validate_leg_for_execute,
)
from enforcement.types import (
    AccountStatusProvider,
    BrokerAccount,
    GateDecision,
    LegProposal,
    OrderIntent,
    OrderSide,
    Proposal,
)

__all__ = [
    "AccountStatusProvider",
    "AuditChainBroken",
    "AuditEntry",
    "AuditLog",
    "BrokerAccount",
    "CircuitOpen",
    "DailyLimitExceeded",
    "EnforcementCore",
    "FrozenTicker",
    "GateDecision",
    "GateError",
    "IntentMismatch",
    "LegProposal",
    "LiveOrderRequiresConfirmation",
    "OrderIntent",
    "OrderSide",
    "PerOrderLimitExceeded",
    "Proposal",
    "ProposalStore",
    "ReconciliationDivergence",
    "TokenAlreadyUsed",
    "TokenExpired",
    "TokenInvalid",
    "gate_order",
    "idempotency_key",
    "intent_hash",
    "propose_fanout",
    "validate_leg_for_execute",
]
