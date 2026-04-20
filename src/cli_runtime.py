from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum
from typing import Any
from uuid import uuid4


class ExitCode(IntEnum):
    SUCCESS = 0
    INVALID_ARGS = 2
    CONFIG_CREDENTIAL_MISSING = 3
    AUTH_SESSION_FAILURE = 4
    PARTIAL_BROKER_FAILURE = 5
    FULL_BROKER_FAILURE = 6
    NON_INTERACTIVE_INPUT_REQUIRED = 7
    SWEEP_NO_SHARES_FOUND = 8
    INTERNAL_ERROR = 10


@dataclass(slots=True)
class ExecutionContext:
    command: str | None
    output_format: str = "text"
    non_interactive: bool = False
    dry_run: bool = False
    mock_brokers: bool = False
    log_format: str = "text"
    log_file: str = ""
    request_id: str = ""

    def __post_init__(self) -> None:
        if not self.request_id:
            self.request_id = f"req_{uuid4().hex[:12]}"


@dataclass(slots=True)
class CliRuntimeError(Exception):
    message: str
    exit_code: ExitCode
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message


def build_response_envelope(
    *,
    ok: bool,
    command: str | None,
    request_id: str,
    data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "command": command,
        "request_id": request_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "data": data or {},
        "warnings": warnings or [],
        "errors": errors or [],
    }


def compute_trade_exit_code(results: dict[str, Any]) -> ExitCode:
    successful = int(results.get("successful", 0))
    failed = int(results.get("failed", 0))
    skipped = int(results.get("skipped", 0))

    if failed > 0 and successful > 0:
        return ExitCode.PARTIAL_BROKER_FAILURE
    if failed > 0 and successful == 0:
        return ExitCode.FULL_BROKER_FAILURE
    if skipped > 0 and successful == 0 and failed == 0:
        return ExitCode.CONFIG_CREDENTIAL_MISSING
    return ExitCode.SUCCESS
