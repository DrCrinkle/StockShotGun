"""Per-broker MCP module for Fennel.

Auto-generated F2a scaffold. The SPEC binds this broker's name to the
existing async functions in `brokers.fennel`; the shared BrokerMCPServer
factory in `agentic._base` wraps SPEC into the canonical MCP tool surface
(`place_at_broker`, `get_holdings_at_broker`, `health_check`).
"""

from __future__ import annotations

from brokers import fennelTrade, fennelGetHoldings

from agentic._base import BrokerMCPSpec, make_session_accounts_fn

SPEC = BrokerMCPSpec(
    name="Fennel",
    trade_fn=fennelTrade,
    holdings_fn=fennelGetHoldings,
    validate_fn=None,
    # F3 v0.4 — Fennel session_manager caches account_ids during init; this
    # surfaces those to the agentic fan-out so all Fennel accounts (typically
    # 3+ per user) participate instead of collapsing to a single "primary" leg.
    list_accounts_fn=make_session_accounts_fn("Fennel"),
    requires_mfa=False,
    supports_fractional=False,
    notes="Personal access token",
)

__all__ = ["SPEC"]
