"""Back-compat shim (ADR 0006 step 2).

Telemetry moved to `execution/telemetry.py` — it is cross-cutting observability
used by both the execution engine and the broker runtime, not MCP-specific, so
it belongs in the neutral `execution/` layer. This module re-exports the public
and (test-referenced) private names so existing `from agentic._telemetry import …`
callers keep working until they are repointed.
"""

from __future__ import annotations

from execution.telemetry import (  # noqa: F401
    TelemetryLog,
    _redact,
    _summarize_result,
    _truncate,
    configure_telemetry_log,
    logged_tool,
    reset_telemetry_log,
    telemetry_log,
)

__all__ = [
    "TelemetryLog",
    "configure_telemetry_log",
    "logged_tool",
    "reset_telemetry_log",
    "telemetry_log",
]
