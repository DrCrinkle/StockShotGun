"""Order-execution context shared across the app.

The `OrderBatchProcessor` direct-broker fan-out (and its pre-flight
`_validate_brokers` step) was retired in the F5 v0.4 migration to the Router
— execution now goes through `agentic.cli_bridge.execute_via_router`, and
pre-flight validation lives in `agentic.cli_bridge.preflight_validate`. Only
the per-broker context variable survives, consumed by `tui/response_handler`
to label output with the broker currently executing.
"""

import contextvars

# Tracks which broker is currently executing (read by tui/response_handler).
current_broker: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_broker", default=None
)
