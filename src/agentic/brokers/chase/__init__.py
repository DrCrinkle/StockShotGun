"""Per-broker MCP module for Chase.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.chase`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import chaseTrade, chaseGetHoldings

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Chase",
    trade_fn=chaseTrade,
    holdings_fn=chaseGetHoldings,
    validate_fn=None,
    requires_mfa=False,
    supports_fractional=False,
    notes="Browser automation",
)

__all__ = ["SPEC"]
