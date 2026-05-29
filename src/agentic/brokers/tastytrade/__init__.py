"""Per-broker MCP module for TastyTrade.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.tasty`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import tastyTrade, tastyGetHoldings, tastyValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="TastyTrade",
    trade_fn=tastyTrade,
    holdings_fn=tastyGetHoldings,
    validate_fn=tastyValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="Username/password",
)

__all__ = ["SPEC"]
