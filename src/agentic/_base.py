"""Per-broker MCP transport + back-compat shim (ADR 0006 step 2).

The broker *runtime* (`InProcessBroker`/`BrokerMCPServer`, `BrokerMCPSpec`,
`PlaceResult`, the spec builder and account-discovery helpers) moved to the
neutral `execution/in_process.py`. This module now holds only the MCP *transport*
— the FastMCP tool wrapping and stdio entrypoint, which genuinely belong to the
agentic adapter — and re-exports the runtime symbols so existing
`from agentic._base import …` callers keep working unchanged.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from execution.in_process import (  # noqa: F401 — re-exported for back-compat
    BrokerMCPServer,
    BrokerMCPSpec,
    HoldingsFn,
    InProcessBroker,
    ListAccountsFn,
    PlaceResult,
    TradeFn,
    ValidateFn,
    _default_single_account,
    build_broker_mcp_spec,
    make_session_accounts_fn,
    session_manager_accounts,
)


def _result_to_dict(r: PlaceResult) -> dict[str, Any]:
    """PlaceResult → JSON-RPC-safe dict. Used at the MCP tool boundary because
    dataclass instances don't serialize cleanly over the wire.
    """
    return {
        "ok": r.ok,
        "broker": r.broker,
        "account_id": r.account_id,
        "idempotency_key": r.idempotency_key,
        "dry_run": r.dry_run,
        "reason": r.reason,
        "detail": r.detail,
        "fill_qty": r.fill_qty,
        "fill_price": r.fill_price,
    }


def build_fastmcp_server(
    spec: BrokerMCPSpec,
    core: Any | None = None,
) -> Any:
    """Wrap a BrokerMCPSpec into a FastMCP server with the four canonical tools.

    The MCP SDK is imported lazily so the agentic package remains importable
    on machines without the `mcp` dep installed (e.g., for offline unit tests
    of broker SPECs).
    """
    from mcp.server.fastmcp import FastMCP

    server = InProcessBroker(spec, core=core)
    app = FastMCP(name=f"ssg-{spec.name.lower()}")

    @app.tool()
    async def place_at_broker(
        ticker: str,
        qty: float,
        side: str,
        account_id: str,
        dry_run: bool,
        confirmation_token: str,
        price: float | None = None,
    ) -> dict[str, Any]:
        """Place one leg of an order at this broker.

        Documented as router-internal — only the StockShotGun router MCP and
        the enforcement library are expected callers. Re-validates the gate
        regardless of caller (defense in depth).
        """
        result = await server.place_at_broker(
            ticker=ticker,
            qty=qty,
            side=side,
            price=price,
            account_id=account_id,
            dry_run=dry_run,
            confirmation_token=confirmation_token,
        )
        return _result_to_dict(result)

    @app.tool()
    async def get_holdings_at_broker(ticker: str | None = None) -> dict[str, Any]:
        """Return this broker's holdings (optionally filtered to one ticker)."""
        raw = await server.get_holdings_at_broker(ticker)
        return {"broker": spec.name, "ticker": ticker, "holdings": raw}

    @app.tool()
    async def list_accounts_at_broker() -> dict[str, Any]:
        """Return the account_ids the router should fan out across for this broker."""
        return {"broker": spec.name, "account_ids": await server.list_accounts_at_broker()}

    @app.tool()
    async def health_check() -> dict[str, Any]:
        """Per-broker MCP liveness + breaker + capability flags."""
        return await server.health_check()

    return app


def run_stdio(spec: BrokerMCPSpec) -> None:
    """Entrypoint each broker's `__main__.py` calls.

    Default transport is stdio (Claude Desktop, most MCP clients). Set
    `SSG_AGENTIC_SMOKE_ONLY=1` to skip starting the server and just print
    the health-check dict — useful for module-loads-cleanly probes in CI
    without hanging on stdin.
    """
    if os.getenv("SSG_AGENTIC_SMOKE_ONLY") == "1":
        asyncio.run(_smoke_health(spec))
        return
    app = build_fastmcp_server(spec)
    app.run()  # stdio transport by default


async def _smoke_health(spec: BrokerMCPSpec) -> None:
    import json

    health = await InProcessBroker(spec).health_check()
    print(json.dumps(health, indent=2))


# Backwards-compatible alias for the previous scaffold name.
run_smoke = run_stdio
