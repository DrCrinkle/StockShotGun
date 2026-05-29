"""Per-broker MCP module for Robinhood.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.robin`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import robinTrade, robinGetHoldings, robinValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Robinhood",
    trade_fn=robinTrade,
    holdings_fn=robinGetHoldings,
    validate_fn=robinValidate,
    requires_mfa=True,
    supports_fractional=False,
    notes="Username/password + MFA",
)

__all__ = ["SPEC"]
