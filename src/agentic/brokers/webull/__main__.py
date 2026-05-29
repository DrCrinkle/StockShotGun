"""Entrypoint for `python -m agentic.brokers.webull`.

Runs the broker MCP server. Until the `mcp` SDK is wired into deps and the
shared MCP-protocol runtime lands, this executes a smoke health check and
exits — proving the package and SPEC load cleanly.
"""

from __future__ import annotations

from agentic.brokers.webull import SPEC
from agentic._base import run_smoke

if __name__ == "__main__":
    run_smoke(SPEC)
