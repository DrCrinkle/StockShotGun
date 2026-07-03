"""Execution layer — the core order-execution engine (ADR 0006).

`ExecutionEngine` is the engine the CLI, TUI, operator CLI, and MCP router all
sit on top of as thin adapters. This package is the canonical import home:

    from execution import ExecutionEngine

ADR 0006 step 2 (import-direction flip complete): the class body now lives
here, in `execution/engine.py`, which imports only from `execution/`,
`enforcement/`, `brokers/`, and stdlib — zero imports from `agentic/`. The
import direction is `agentic/ -> execution/`, not the other way around.
`agentic/router/_server.py` re-exports these names for back-compat.

`Router` remains exported as a back-compat alias for callers not yet repointed.
"""

from __future__ import annotations

from execution.engine import ExecutionEngine, Router

__all__ = ["ExecutionEngine", "Router"]
