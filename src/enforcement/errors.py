from __future__ import annotations

from cli_runtime import CliRuntimeError, ExitCode


class GateError(CliRuntimeError):
    """Base class for enforcement-layer rejections.

    Subclasses MUST set a stable `reason` code (snake_case) — agents, logs,
    and tests key off this string. Never include credentials in the message.
    """

    reason: str = "gate_error"

    def __init__(self, message: str, exit_code: ExitCode = ExitCode.INVALID_ARGS):
        super().__init__(message=message, exit_code=exit_code)


class IntentMismatch(GateError):
    reason = "intent_mismatch"


class TokenInvalid(GateError):
    reason = "token_invalid"


class TokenExpired(GateError):
    reason = "token_expired"


class TokenAlreadyUsed(GateError):
    reason = "token_already_used"


class PerOrderLimitExceeded(GateError):
    reason = "per_order_limit_exceeded"


class DailyLimitExceeded(GateError):
    reason = "daily_limit_exceeded"


class FrozenTicker(GateError):
    reason = "frozen_ticker"


class CircuitOpen(GateError):
    reason = "circuit_open"


class ReconciliationDivergence(GateError):
    reason = "reconciliation_divergence"


class LiveOrderRequiresConfirmation(GateError):
    reason = "live_order_requires_confirmation"


class AuditChainBroken(GateError):
    reason = "audit_chain_broken"
