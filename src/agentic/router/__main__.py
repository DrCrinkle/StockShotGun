"""Entrypoint: `python -m agentic.router` starts the router MCP stdio server."""

from __future__ import annotations

from agentic.router import run_stdio

if __name__ == "__main__":
    run_stdio()
