"""FastMCP wiring for the router MCP (ADR 0006 step 2).

The `ExecutionEngine` class body — plus the store-path constants, account
status providers, and `load_all_broker_specs` — now live in canonical form in
`execution/engine.py`. This module holds only the MCP *transport*
(`build_router_fastmcp_server`) and the stdio entrypoint glue (`run_stdio`),
and re-exports the engine names so existing `from agentic.router._server import
...` / `from agentic.router import ...` callers keep working unchanged.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from execution.engine import (  # noqa: F401 — re-exported for back-compat
    DEFAULT_AUTOMATION_STORE_PATH,
    DEFAULT_PLACEHOLDER_ACCOUNT_ID,
    DEFAULT_RSA_STORE_PATH,
    BrokerServerAccountStatusProvider,
    ExecutionEngine,
    NullAccountStatusProvider,
    Router,
    load_all_broker_specs,
    sanitize_holdings,
)
from enforcement import gate_order  # noqa: F401 — back-compat re-export only; the engine's call site reads execution.engine.gate_order


def build_router_fastmcp_server(router: ExecutionEngine) -> Any:
    """Wrap an ExecutionEngine into a FastMCP server with the agent-facing tools."""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name="ssg-router")

    @app.tool()
    async def list_brokers() -> dict[str, Any]:
        """List enabled brokers with health, MFA, fractional-support flags."""
        return await router.list_brokers()

    @app.tool()
    async def get_holdings(
        ticker: str | None = None,
        brokers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fan out a holdings query across selected brokers (or all by default)."""
        return await router.get_holdings(ticker=ticker, brokers=brokers)

    @app.tool()
    async def get_rsa_trade(trade_id: int) -> dict[str, Any]:
        """Return the rsa_trades row + all rsa_positions rows for an RSA trade.

        Read-only — the agent uses this to know what ticker, ratio, expected
        split date, and per-broker pre-split quantities are in flight.
        """
        return await router.get_rsa_trade(trade_id=trade_id)

    @app.tool()
    async def run_sweep(trade_id: int, dry_run: bool = True) -> dict[str, Any]:
        """Classify every position in an RSA trade against current broker
        holdings. Returns per-position `resolved_status` (share_arrived,
        processing, ambiguous, awaiting_split, fractional_pending, error)
        and a summary count.

        v0.2: `dry_run=False` ALSO writes each classification to `sweep_state`
        via the canonical `rsa_store.record_sweep` primitive — the agentic
        sweep is now feature-parity with the legacy `python3 main.py sweep
        --from-trade <id>` CLI.
        """
        return await router.run_sweep(trade_id=trade_id, dry_run=dry_run)

    @app.tool()
    async def recap_ingest(recap_text: str) -> dict[str, Any]:
        """Parse a chat recap and persist all four signal tiers.

        Tiers:
          1. UPCOMING BUYS (date + ratio known) → buy_signals → due-buy queue
          2. STOCKS BACK AND LATEST → stock_back_state → pending sell triggers
          3. RESEARCH POSTED (date soft) → research_signals (watchlist)
          4. TBA (ratio/date pending) → tba_candidates (long watchlist)

        Returns counts + the categorized lists so the agent can act on
        each tier without a second call.
        """
        return await router.recap_ingest(recap_text=recap_text)

    @app.tool()
    async def scan_signals(refresh: bool = True) -> dict[str, Any]:
        """Scan the Nasdaq reverse-split calendar into the signal store
        (refresh=True) or read staged 'new' signals (refresh=False).
        Read/ingest only — never trades. Evaluate each returned signal and
        either promote_signal (worth playing) or dismiss_signal (with reason).

        Signals with a null effective_date become immediately due if
        promoted — verify the real split date before promoting one.

        If the calendar fetch/parse step fails, returns
        `{"ok": False, "error": ..., "source": ...}` instead of raising.
        """
        return await router.scan_signals(refresh=refresh)

    @app.tool()
    async def dismiss_signal(signal_id: int, reason: str) -> dict[str, Any]:
        """Dismiss a staged calendar signal that isn't worth playing. Always
        give a concrete reason (e.g. 'ratio below 1:5', 'price exceeds
        per-order cap') — it's the audit trail for why the agent skipped a
        play. Only 'new' signals can be dismissed; returns ok=false with an
        error message otherwise."""
        return await router.dismiss_signal(signal_id=signal_id, reason=reason)

    @app.tool()
    async def promote_signal(signal_id: int) -> dict[str, Any]:
        """Promote a calendar signal into the automate due-buy queue. The buy
        is NOT executed by this call, but it will be gated and executed on
        the next automate run — and a signal without an effective date
        becomes immediately due. Dismiss instead if unsure."""
        return await router.promote_signal(signal_id=signal_id)

    @app.tool()
    async def sell_arrived(
        trade_id: int, price: float | None = None
    ) -> dict[str, Any]:
        """One-shot tool: run a sweep and PROPOSE sells for every leg
        classified as `share_arrived`. Returns a `proposal_id` the agent
        passes to `execute_order` after principal review. Live sells still
        require the explicit two-step propose/execute flow with `--live` —
        this tool only proposes.
        """
        return await router.sell_arrived(trade_id=trade_id, price=price)

    @app.tool()
    async def propose_order(
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | None = None,
        price: float | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Run safety gates and mint a single-use confirmation_token.

        Surface the returned token + estimate to the principal. The token
        expires (default 300s) and is bound to this exact intent — any change
        to ticker/qty/side/brokers/price invalidates it.
        """
        return await router.propose_order(
            ticker=ticker,
            qty=qty,
            side=side,
            brokers=brokers,
            price=price,
            dry_run=dry_run,
        )

    @app.tool()
    async def execute_order(
        proposal_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Place the order fan-out using a previously minted proposal_id.

        The router looks up the per-leg tokens from the proposal and
        dispatches each broker leg with its own single-use token. Per-leg
        failures isolate; sibling legs continue.
        """
        return await router.execute_order(
            proposal_id=proposal_id,
            dry_run=dry_run,
        )

    @app.tool()
    async def place_order(
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | None = None,
        price: float | None = None,
        dry_run: bool = True,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Convenience tool — proposes + executes a dry-run in one call, or
        executes with a supplied proposal_id. Live orders without a proposal_id
        are rejected.
        """
        return await router.place_order(
            ticker=ticker,
            qty=qty,
            side=side,
            brokers=brokers,
            price=price,
            dry_run=dry_run,
            proposal_id=proposal_id,
        )

    return app


def run_stdio() -> None:
    """`python -m agentic.router` entrypoint.

    Set `SSG_AGENTIC_SMOKE_ONLY=1` to print the broker health dict and exit
    without starting the stdio server — useful for CI / load-check scripts.
    """
    router = ExecutionEngine.from_all_brokers()
    if os.getenv("SSG_AGENTIC_SMOKE_ONLY") == "1":
        import json

        health = asyncio.run(router.list_brokers())
        print(json.dumps(health, indent=2))
        return
    app = build_router_fastmcp_server(router)
    app.run()
