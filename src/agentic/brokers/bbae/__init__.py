"""Per-broker MCP module for BBAE.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.bbae`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import bbaeTrade, bbaeGetHoldings, bbaeValidate

from agentic._base import BrokerMCPSpec

SPEC = BrokerMCPSpec(
    name="BBAE",
    trade_fn=bbaeTrade,
    holdings_fn=bbaeGetHoldings,
    validate_fn=bbaeValidate,
    requires_mfa=False,
    supports_fractional=False,
    notes="May require CAPTCHA/OTP",
)

__all__ = ["SPEC"]
