"""Per-broker MCP modules.

Each subpackage corresponds to one of the 13 enabled brokers in
`brokers.base.BrokerConfig.BROKERS`. Each exports a `SPEC: BrokerMCPSpec` and
has a `__main__.py` so it can be run as `python -m agentic.brokers.<broker>`.
"""

from __future__ import annotations

from agentic._base import (
    BrokerMCPServer,
    BrokerMCPSpec,
    PlaceResult,
    build_fastmcp_server,
    run_smoke,
    run_stdio,
)

__all__ = [
    "BrokerMCPServer",
    "BrokerMCPSpec",
    "PlaceResult",
    "build_fastmcp_server",
    "run_smoke",
    "run_stdio",
]
