"""In-process broker runtime (ADR 0006 step 2; formerly agentic/_base.py).

Each broker is described by a `BrokerMCPSpec` (built from the registry's pure-data
`BrokerSpec`). `InProcessBroker` wraps the broker's `brokers.<broker>.{Trade,
GetHoldings}` functions with: (a) per-broker rate limiting, (b) enforcement-leg
validation (intent-binding + token check), (c) per-broker circuit-breaker
success/failure recording, (d) audit-log emission via EnforcementCore.

Defense in depth: the engine has already called `EnforcementCore.propose_order`
and minted a token. `InProcessBroker` receives that token and re-validates it via
`EnforcementCore.gate_execute_leg` — it does not trust the engine.

This module imports nothing from `agentic/` — that is the point of the move: the
broker runtime is a neutral execution concern, satisfying `execution.ports.BrokerPort`.
The MCP *transport* (FastMCP tool wrapping, stdio) stays in `agentic/_base.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from brokers import registry
from brokers.base import rate_limiter
from enforcement import (
    BrokerAccount,
    EnforcementCore,
    GateError,
    OrderIntent,
    OrderSide,
)
from execution.telemetry import logged_tool

# Blind shape: (side, qty, ticker, price) — the common contract every trade
# fn satisfies. Account-scoped trade fns (spec.account_scoped_trade=True,
# e.g. Fennel) ADDITIONALLY accept an `account_id: str` keyword-only arg;
# `place_at_broker` passes it only when the flag is set, so this Callable
# alias undersells account-scoped fns' real signature but keeps the simpler
# shape as the documented default every new broker starts from.
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
    This helper surfaces that list so multi-account fan-out works without each
    broker needing its own discovery code.

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
    """Per-broker runtime configuration.

    Built from the registry's pure-data `BrokerSpec` via `build_broker_mcp_spec`
    (ADR 0004) — resolving the function refs imports only that broker's module.
    """

    name: str
    trade_fn: TradeFn
    holdings_fn: HoldingsFn
    validate_fn: ValidateFn | None = None
    list_accounts_fn: ListAccountsFn = _default_single_account
    requires_mfa: bool = False
    supports_fractional: bool = False
    # True only when `trade_fn` can place on ONE specific account per call.
    # When True, `place_at_broker` calls
    # `trade_fn(side, qty, ticker, price, account_id=account_id)` — the
    # leg's own account, no internal fan-out. When False (the default —
    # every broker except Fennel), `trade_fn` is account-blind
    # (`TradeFn(side, qty, ticker, price)`, no account kwarg): dispatching a
    # leg with a real (non-"primary") account_id to it cannot target that
    # account, and for internally-fanning fns it multiplies orders
    # (final-review C1). `place_at_broker` fails such legs loudly with
    # reason="account_scoped_dispatch_unsupported" instead of placing blind.
    # `build_broker_mcp_spec` threads this straight from the registry's
    # `BrokerSpec.account_scoped_trade` (ADR 0006 completion) — Fennel is
    # the first broker with it True; the guard below still protects the
    # other 12 (all still account-blind). Tests may also set it on fakes
    # that simulate a not-yet-migrated broker.
    account_scoped_trade: bool = False
    notes: str = ""


def build_broker_mcp_spec(spec: "registry.BrokerSpec") -> BrokerMCPSpec:
    """Build the runtime ``BrokerMCPSpec`` from a pure-data registry ``BrokerSpec``.

    Resolving the function refs imports the broker module — so building one
    spec imports only that broker, not all thirteen (ADR 0004). The registry's
    ``multi_account`` flag maps to the session-manager-backed account discovery
    closure; everyone else fans out a single ``"primary"`` leg.

    ``account_scoped_trade`` threads straight through from the registry's
    ``BrokerSpec.account_scoped_trade`` (ADR 0006 completion). It defaults to
    False for every broker whose trade fn is still account-blind
    (``TradeFn(side, qty, ticker, price)``, no account kwarg) — dispatching a
    real-account-id leg to one of those would multiply orders for
    internally-fanning fns (final-review C1), so ``place_at_broker``'s guard
    keeps rejecting them. Fennel is the first broker to flip it True: its
    trade fn now accepts an ``account_id`` kwarg and places exactly one
    order per call when given one.
    """
    return BrokerMCPSpec(
        name=spec.name,
        trade_fn=registry.resolve_trade(spec.name),
        holdings_fn=registry.resolve_holdings(spec.name),
        validate_fn=registry.resolve_validate(spec.name),
        list_accounts_fn=(
            make_session_accounts_fn(spec.name)
            if spec.multi_account
            else _default_single_account
        ),
        account_scoped_trade=spec.account_scoped_trade,
        requires_mfa=spec.requires_mfa,
        supports_fractional=spec.supports_fractional,
        notes=spec.notes,
    )


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


class InProcessBroker:
    """In-process per-broker runtime (formerly `BrokerMCPServer`).

    A thin layer wrapping the existing `brokers.<broker>.{Trade,GetHoldings}`
    functions with: (a) per-broker rate limiting, (b) enforcement-leg
    validation (intent-binding + token check), (c) per-broker circuit-breaker
    success/failure recording, (d) audit-log emission via EnforcementCore.

    Satisfies `execution.ports.BrokerPort`. The FastMCP/stdio transport that
    exposes these methods as MCP tools lives in `agentic/_base.py`.
    """

    def __init__(self, spec: BrokerMCPSpec, core: EnforcementCore | None = None):
        self.spec = spec
        self.core = core or EnforcementCore.from_default_paths()

    @logged_tool(tool="health_check")
    async def health_check(self) -> dict[str, Any]:
        """Return broker health: rate-limit budget, breaker state, MFA flag.

        Read-only. The engine calls this when populating `list_brokers()`.
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

        Per-leg-token model: `confirmation_token` is a LEG token (one per
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
        # Account-scoped dispatch guard (final-review C1). A leg addressed to
        # a REAL account (not the "primary" placeholder every single-account
        # discovery path assigns) cannot be honored by an account-blind
        # trade_fn: the fn can't target that account, and internally-fanning
        # fns would place once per session account PER LEG —
        # N accounts x N legs = N^2 live orders. Fail the leg loudly instead
        # of silently placing account-blind. Applies to dry-run legs too so
        # rehearsals predict live behavior. Fennel completed the migration
        # (ADR 0006 completion, P1 fix) — its spec now sets
        # `account_scoped_trade=True`, so its real-account legs pass this
        # guard and dispatch through the account_id-keyword path below. The
        # other 12 registry specs remain account-blind (`multi_account=False`
        # for all of them today), so the guard still protects them and stays
        # ready for any future broker that flips `multi_account=True` before
        # its trade fn accepts `account_id`.
        if account_id and account_id != "primary" and not self.spec.account_scoped_trade:
            return PlaceResult(
                ok=False,
                broker=self.spec.name,
                account_id=account_id,
                idempotency_key="",
                dry_run=dry_run,
                reason="account_scoped_dispatch_unsupported",
                detail=(
                    f"{self.spec.name}'s trade fn is account-blind (no account "
                    f"parameter); refusing to place leg for account "
                    f"{account_id!r} — it would dispatch to the broker's "
                    f"default/all accounts, not this one"
                ),
            )
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
            # Account-scoped dispatch (ADR 0006 completion): pass this leg's
            # own account_id ONLY when the spec declares its trade fn
            # accepts it. Blind trade fns (every broker except Fennel today)
            # are never called with the kwarg, so they stay exactly as they
            # were pre-migration — this is additive, not a signature change
            # for them.
            if self.spec.account_scoped_trade:
                # `TradeFn`'s declared type is the blind 4-arg shape (see the
                # alias's docstring above); account-scoped trade fns
                # additionally accept `account_id` by convention, not by
                # type — this is the one call site that relies on that
                # convention, gated on the spec flag.
                await self.spec.trade_fn(  # type: ignore[call-arg]
                    side, qty, ticker, price, account_id=account_id
                )
            else:
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


# Back-compat alias (ADR 0006 step 2): the class was renamed
# BrokerMCPServer → InProcessBroker. Callers and tests still import
# `BrokerMCPServer`; this keeps them working until they are repointed.
BrokerMCPServer = InProcessBroker
