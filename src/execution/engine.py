"""Canonical home of the execution engine (ADR 0006 step 1).

The engine class body still lives in `agentic/router/_server.py` for now; this
module re-exports it under its canonical name so new code imports the stable
location:

    from execution.engine import ExecutionEngine

Step 2 will move the class body here and invert the dependency so this module
no longer imports from `agentic/`. `Router` is the back-compat alias.
"""

from __future__ import annotations

from agentic.router._server import ExecutionEngine, Router

__all__ = ["ExecutionEngine", "Router"]
