"""Per-broker MCP module for Schwab.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.schwab`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import schwabTrade, schwabGetHoldings, schwabValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Schwab",
    trade_fn=schwabTrade,
    holdings_fn=schwabGetHoldings,
    validate_fn=schwabValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="OAuth 2.0; token cached in tokens/",
)

__all__ = ["SPEC"]
