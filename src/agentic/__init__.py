"""StockShotGun agentic / MCP layer.

This package is the agent-callable boundary of StockShotGun. Two sub-packages:

- `agentic.brokers.<broker>` — twelve per-broker MCP servers, one per broker,
  each owning its credentials, rate limiter, and circuit-breaker instance. Each
  exposes `place_at_broker`, `get_holdings_at_broker`, `health_check` —
  documented as router-only (defense in depth: the broker MCP re-validates
  enforcement and does not trust its caller).
- `agentic.router` — the agent-facing fan-out MCP. Exposes `place_order`,
  `get_holdings`, `list_brokers`, `run_sweep`, `propose_order`, `execute_order`.
  Calls per-broker MCPs; never reaches into broker SDKs directly.

The package name is `agentic` (not `mcp`) deliberately — `mcp` would shadow the
third-party `mcp` Python SDK and break any code that imports the SDK by that
name.
"""

from __future__ import annotations
