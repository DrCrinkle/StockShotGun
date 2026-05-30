"""StockShotGun agentic / MCP layer.

This package is the agent-callable boundary of StockShotGun. Two parts:

- `agentic.broker` — the generic per-broker MCP entrypoint, run as
  `python -m agentic.broker <BrokerName>`. It resolves one broker from
  `brokers.registry` (lazily, so only that broker imports) and serves
  `place_at_broker`, `get_holdings_at_broker`, `health_check` — documented as
  router-only (defense in depth: the broker MCP re-validates enforcement and
  does not trust its caller). See ADR 0004.
- `agentic.router` — the agent-facing fan-out MCP. Exposes `place_order`,
  `get_holdings`, `list_brokers`, `run_sweep`, `propose_order`, `execute_order`.
  Calls per-broker MCPs; never reaches into broker SDKs directly.

The package name is `agentic` (not `mcp`) deliberately — `mcp` would shadow the
third-party `mcp` Python SDK and break any code that imports the SDK by that
name.
"""

from __future__ import annotations
