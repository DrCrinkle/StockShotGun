"""Shared base for per-broker MCP servers.

Each broker MCP module exports a single `BrokerMCPSpec` describing its broker
name, the async trade/holdings functions to call (from `brokers.*`), and the
optional dry-run validate function. The `BrokerMCPServer` class then exposes
the three canonical tools — `place_at_broker`, `get_holdings_at_broker`,
`health_check` — wrapping each in the enforcement gate.

Defense in depth: the router has already called `EnforcementCore.propose_order`
and minted a token. The per-broker MCP receives that token and re-validates it
via `EnforcementCore.gate_execute_leg`. The broker MCP does not trust the router.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from agentic._telemetry import logged_tool
from brokers.base import rate_limiter
from enforcement import (
    BrokerAccount,
    EnforcementCore,
    GateDecision,
    GateError,
    OrderIntent,
    OrderSide,
)
from enforcement.intent import idempotency_key as _compute_idem_key

TradeFn = Callable[[str, float, str, float | None], Awaitable[Any]]
HoldingsFn = Callable[[str | None], Awaitable[Any]]
ValidateFn = Callable[[str, float, str, float | None], Awaitable[Any]]
ListAccountsFn = Callable[[], Awaitable[list[str]]]


async def _default_single_account() -> list[str]:
    """Default `list_accounts_fn` — single-account placeholder.

    A broker SPEC that doesn't override this gets one synthetic account named
    `"primary"`. Brokers with real multi-account support (Robinhood IRA +
    taxable, Wells Fargo WELLSTRADE + IRAs) supply their own coroutine that
    queries `brokers.session_manager` and returns the discovered account_ids.
    """
    return ["primary"]


async def session_manager_accounts(broker_name: str) -> list[str]:
    """Read `account_ids` from the broker's cached session dict.

    Many broker session initializers (Fennel, Robinhood, Schwab, Webull, etc.)
    populate a `session["account_ids"]` field during `initialize_selected_sessions`.
    This helper surfaces that list to the agentic layer so multi-account fan-out
    works without each broker needing its own discovery code.

    Returns `["primary"]` (default placeholder) when:
      - The broker isn't in `BrokerConfig` or has no session_key
      - The session hasn't been initialized yet
      - The session dict has no `account_ids` field
      - `account_ids` is empty

    `["primary"]` keeps `propose_order` working with a default leg even when
    real account discovery hasn't run — the agent's first call typically
    initializes the session, populating account_ids for subsequent calls.
    """
    try:
        from brokers.base import BrokerConfig  # type: ignore[import-untyped]
        from brokers.session_manager import session_manager  # type: ignore[import-untyped]
    except ImportError:
        return ["primary"]

    session_key = BrokerConfig.get_session_key(broker_name)
    if not session_key:
        return ["primary"]
    session = session_manager.sessions.get(session_key)
    if not session or not isinstance(session, dict):
        return ["primary"]
    account_ids = session.get("account_ids", [])
    if not account_ids:
        return ["primary"]
    return [str(a) for a in account_ids]


def make_session_accounts_fn(broker_name: str) -> ListAccountsFn:
    """Build a `list_accounts_fn` closure bound to a broker name. Use in a
    broker SPEC: `list_accounts_fn=make_session_accounts_fn("Fennel")`.

    The closure reads from `brokers.session_manager.sessions[session_key]`
    at every call, so account changes between sessions (e.g., a new IRA
    added at the broker) surface on the next call without restart.
    """

    async def _fn() -> list[str]:
        return await session_manager_accounts(broker_name)

    return _fn


@dataclass(frozen=True)
class BrokerMCPSpec:
    """Per-broker configuration for the MCP-server factory.

    Each broker module under `agentic/brokers/<broker>/__init__.py` exports a
    module-level `SPEC = BrokerMCPSpec(...)`. The factory reads this to build
    the broker MCP server.
    """

    name: str
    trade_fn: TradeFn
    holdings_fn: HoldingsFn
    validate_fn: ValidateFn | None = None
    list_accounts_fn: ListAccountsFn = _default_single_account
    requires_mfa: bool = False
    supports_fractional: bool = False
    notes: str = ""


@dataclass
class PlaceResult:
    ok: bool
    broker: str
    account_id: str
    idempotency_key: str
    dry_run: bool
    reason: str | None = None
    detail: str | None = None
    fill_qty: float | None = None
    fill_price: float | None = None


class BrokerMCPServer:
    """In-process per-broker MCP surface.

    A thin layer wrapping the existing `brokers.<broker>.{Trade,GetHoldings}`
    functions with: (a) per-broker rate limiting, (b) enforcement-leg
    validation (intent-binding + token check), (c) per-broker circuit-breaker
    success/failure recording, (d) audit-log emission via EnforcementCore.

    The actual MCP-protocol wrapping (FastMCP tool decorators, stdio transport,
    JSON-RPC) is added in a thin sibling module once the `mcp` SDK is in deps.
    The methods below are the canonical tool implementations; the SDK layer
    only adapts I/O shapes.
    """

    def __init__(self, spec: BrokerMCPSpec, core: EnforcementCore | None = None):
        self.spec = spec
        self.core = core or EnforcementCore.from_default_paths()

    @logged_tool(tool="health_check")
    async def health_check(self) -> dict[str, Any]:
        """Return broker MCP health: rate-limit budget, breaker state, MFA flag.

        Read-only. The router calls this when populating `list_brokers()`.
        """
        state = self.core.breaker._states.get(self.spec.name)  # noqa: SLF001 — same module pkg
        breaker_open = state.opened_at is not None if state else False
        return {
            "broker": self.spec.name,
            "ok": not breaker_open,
            "breaker_open": breaker_open,
            "requires_mfa": self.spec.requires_mfa,
            "supports_fractional": self.spec.supports_fractional,
            "notes": self.spec.notes,
        }

    @logged_tool(tool="get_holdings_at_broker")
    async def get_holdings_at_broker(self, ticker: str | None = None) -> Any:
        await rate_limiter.wait_if_needed(self.spec.name)
        return await self.spec.holdings_fn(ticker)

    @logged_tool(tool="list_accounts_at_broker")
    async def list_accounts_at_broker(self) -> list[str]:
        """Return the account_ids this broker exposes for fan-out.

        Single-account brokers return `["primary"]` (the default). Multi-account
        brokers override by supplying a `list_accounts_fn` in their SPEC.
        """
        return await self.spec.list_accounts_fn()

    @logged_tool(tool="place_at_broker")
    async def place_at_broker(
        self,
        *,
        ticker: str,
        qty: float,
        side: str,
        price: float | None,
        account_id: str,
        dry_run: bool,
        confirmation_token: str,
    ) -> PlaceResult:
        """Place ONE leg of an order at this broker.

        v0.3 per-leg-token model: `confirmation_token` is a LEG token (one per
        broker+account in the fan-out), bound to a single-target intent
        matching this broker. Validation:
          1. Reconstruct the single-target intent (ticker, side, qty,
             targets=(self_leg,), price, dry_run)
          2. Call `core.gate_execute_leg(leg_token, intent, leg)` which
             atomically consumes the leg token and confirms the leg's bound
             intent_hash matches what was minted at propose_order
          3. On success, record leg outcome with the returned idempotency_key
        Subprocess isolation works through this same path — the trust model
        is the SAME for in-process and subprocess callers.
        """
        intent = OrderIntent(
            ticker=ticker,
            side=OrderSide(side),
            qty=qty,
            targets=(BrokerAccount(self.spec.name, account_id),),
            price=price,
            dry_run=dry_run,
        )
        leg = BrokerAccount(self.spec.name, account_id)
        try:
            decision, _consumed = self.core.gate_execute_leg(
                leg_token=confirmation_token,
                intent=intent,
                leg=leg,
            )
        except GateError as e:
            return PlaceResult(
                ok=False,
                broker=self.spec.name,
                account_id=account_id,
                idempotency_key="",
                dry_run=dry_run,
                reason=e.reason,
                detail=str(e),
            )

        idem = decision.idempotency_key or ""
        if dry_run:
            self.core.record_leg_outcome(
                token=confirmation_token,
                intent=intent,
                leg=leg,
                idempotency_key_value=idem,
                result="ok",
                usd_amount=(price or 0.0) * qty if price else None,
            )
            return PlaceResult(
                ok=True,
                broker=self.spec.name,
                account_id=account_id,
                idempotency_key=idem,
                dry_run=True,
                detail="dry-run accepted",
            )

        await rate_limiter.wait_if_needed(self.spec.name)
        try:
            await self.spec.trade_fn(side, qty, ticker, price)
        except Exception as e:  # SDK exceptions vary widely — surface as a leg-failure
            self.core.record_leg_outcome(
                token=confirmation_token,
                intent=intent,
                leg=leg,
                idempotency_key_value=idem,
                result="error",
                reason="broker_sdk_failure",
                usd_amount=(price or 0.0) * qty if price else None,
            )
            return PlaceResult(
                ok=False,
                broker=self.spec.name,
                account_id=account_id,
                idempotency_key=idem,
                dry_run=False,
                reason="broker_sdk_failure",
                detail=str(e),
            )

        self.core.record_leg_outcome(
            token=confirmation_token,
            intent=intent,
            leg=leg,
            idempotency_key_value=idem,
            result="ok",
            usd_amount=(price or 0.0) * qty if price else None,
        )
        return PlaceResult(
            ok=True,
            broker=self.spec.name,
            account_id=account_id,
            idempotency_key=idem,
            dry_run=False,
            detail="placed",
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
    core: EnforcementCore | None = None,
) -> Any:
    """Wrap a BrokerMCPSpec into a FastMCP server with the three canonical tools.

    The MCP SDK is imported lazily so the agentic package remains importable
    on machines without the `mcp` dep installed (e.g., for offline unit tests
    of broker SPECs).
    """
    from mcp.server.fastmcp import FastMCP

    server = BrokerMCPServer(spec, core=core)
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

    health = await BrokerMCPServer(spec).health_check()
    print(json.dumps(health, indent=2))


# Backwards-compatible alias for the previous scaffold name.
run_smoke = run_stdio
