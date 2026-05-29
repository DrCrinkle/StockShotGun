"""Per-broker MCP module for Webull.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.webull`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import webullTrade, webullGetHoldings, webullValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="Webull",
    trade_fn=webullTrade,
    holdings_fn=webullGetHoldings,
    validate_fn=webullValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="Pre-obtained credentials via Chrome extension",
)

__all__ = ["SPEC"]
