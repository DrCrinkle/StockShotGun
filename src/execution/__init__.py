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
"""

from __future__ import annotations

from execution.engine import ExecutionEngine, Router

__all__ = ["ExecutionEngine", "Router"]
