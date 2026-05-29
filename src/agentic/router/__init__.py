"""Router MCP — the agent-facing fan-out surface.

This is the primary MCP server agents talk to. It exposes the canonical
fan-out tools (`list_brokers`, `get_holdings`, `propose_order`,
`execute_order`) and routes each call to the corresponding per-broker MCP
server. The agent never sees the thirteen underlying broker MCPs.

v0.1 (this module): broker servers live in the same Python process as
the router (in-process call). v0.2 will spawn each broker MCP as a child
process and speak MCP-over-stdio for true blast-radius isolation; the
agent-facing tool surface stays identical across that change.
"""

from __future__ import annotations

from agentic.router._server import (
    BrokerServerAccountStatusProvider,
    NullAccountStatusProvider,
    Router,
    build_router_fastmcp_server,
    run_stdio,
)

__all__ = [
    "BrokerServerAccountStatusProvider",
    "NullAccountStatusProvider",
    "Router",
    "build_router_fastmcp_server",
    "run_stdio",
]
