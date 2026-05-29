"""Per-broker MCP module for Tradier.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.tradier`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import tradierTrade, tradierGetHoldings, tradierValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Tradier",
    trade_fn=tradierTrade,
    holdings_fn=tradierGetHoldings,
    validate_fn=tradierValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="API-token auth",
)

__all__ = ["SPEC"]
