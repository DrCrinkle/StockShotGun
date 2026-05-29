"""Per-broker MCP module for WellsFargo.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.wellsfargo`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import wellsfargoTrade, wellsfargoGetHoldings

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="WellsFargo",
    trade_fn=wellsfargoTrade,
    holdings_fn=wellsfargoGetHoldings,
    validate_fn=None,
    requires_mfa=False,
    supports_fractional=False,
    notes="Browser automation via Zendriver",
)

__all__ = ["SPEC"]
