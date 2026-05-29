"""Per-broker MCP module for Public.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.public`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import publicTrade, publicGetHoldings

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Public",
    trade_fn=publicTrade,
    holdings_fn=publicGetHoldings,
    validate_fn=None,
    requires_mfa=False,
    supports_fractional=False,
    notes="API-token auth",
)

__all__ = ["SPEC"]
