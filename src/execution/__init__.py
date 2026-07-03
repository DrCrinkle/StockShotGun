"""Execution layer — the core order-execution engine (ADR 0006).

`ExecutionEngine` is the engine the CLI, TUI, operator CLI, and MCP router all
sit on top of as thin adapters. This package is the canonical import home:

    from execution import ExecutionEngine

ADR 0006 step 1 (rename + alias): the class still physically lives in
`agentic/router/_server.py` and is re-exported here. Step 2 moves the class body
into `execution/engine.py` and splits the broker runtime behind a `BrokerPort`
protocol, at which point the import direction flips to `agentic/ -> execution/`
(enforced by a test in step 6). Until then this re-export intentionally points
back into `agentic/` — a documented, temporary intermediate.

`Router` remains exported as a back-compat alias for callers not yet repointed.

The re-export is lazy (`__getattr__`, PEP 562) rather than a top-of-module
import: `agentic/router/_server.py` itself imports `execution.in_process`,
`execution.ports`, and `execution.telemetry` as submodules of this package —
which runs THIS `__init__.py` first. An eager `from execution.engine import
...` here would re-enter `_server.py` before its `ExecutionEngine` class
finishes being defined (circular import). Deferring the lookup until an
attribute is actually accessed breaks that cycle without changing what
`from execution import ExecutionEngine` returns to callers.
"""

from __future__ import annotations

from typing import Any

__all__ = ["ExecutionEngine", "Router"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from execution.engine import ExecutionEngine, Router

        return {"ExecutionEngine": ExecutionEngine, "Router": Router}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
