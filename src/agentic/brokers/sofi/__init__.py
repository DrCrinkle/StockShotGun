"""Per-broker MCP module for SoFi.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.sofi`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import sofiTrade, sofiGetHoldings, sofiValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="SoFi",
    trade_fn=sofiTrade,
    holdings_fn=sofiGetHoldings,
    validate_fn=sofiValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="Username/password",
)

__all__ = ["SPEC"]
