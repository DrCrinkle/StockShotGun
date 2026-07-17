from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec, build_broker_mcp_spec
from brokers import registry
from enforcement import (
    AuditLog,
    BrokerAccount,
    EnforcementCore,
    LiveOrderRequiresConfirmation,
    OrderIntent,
    OrderSide,
    ProposalStore,
    gate_order,
    idempotency_key,
)
from enforcement.circuit_breaker import CircuitBreaker


class FakeProvider:
    def get_settled_cash(self, broker: str, account_id: str) -> float:
        return 10_000.0

    def get_day_trades_in_window(self, broker: str, account_id: str) -> int:
        return 0

    def get_observed_qty(self, broker: str, account_id: str, ticker: str) -> float:
        return 0.0


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


@pytest.mark.parametrize("broker_name", registry.all_names())
def test_every_broker_builds_a_spec_from_registry(broker_name: str):
    spec = build_broker_mcp_spec(registry.get(broker_name))
    assert isinstance(spec, BrokerMCPSpec)
    assert spec.name == broker_name
    assert callable(spec.trade_fn)
    assert callable(spec.holdings_fn)
    assert spec.validate_fn is None or callable(spec.validate_fn)


def _fake_spec(name: str = "FakeBroker") -> tuple[BrokerMCPSpec, list[Any]]:
    """Build a spec whose trade_fn records calls instead of hitting an SDK.

    `account_scoped_trade=True` because these tests place legs at a REAL
    account id ("acc1") to exercise token gating / breaker semantics — they
    simulate an account-scoped broker (like Fennel post-ADR-0006-completion).
    `fake_trade` therefore accepts the `account_id` keyword `place_at_broker`
    passes whenever `account_scoped_trade` is True (real trade fns like
    `fennelTrade` take the same kwarg — see `brokers/fennel.py`). The
    account-blind dispatch guard (final-review C1) has its own tests below.
    """
    calls: list[Any] = []

    async def fake_trade(
        side: str, qty: float, ticker: str, price: float | None, account_id: str | None = None
    ) -> Any:
        calls.append(("trade", side, qty, ticker, price, account_id))
        return {"ok": True, "fill_qty": qty, "fill_price": price or 0.0}

    async def fake_holdings(ticker: str | None = None) -> Any:
        calls.append(("holdings", ticker))
        return {ticker or "ALL": 0.0}

    spec = BrokerMCPSpec(
        name=name,
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
        validate_fn=None,
        account_scoped_trade=True,
    )
    return spec, calls


def test_dry_run_place_does_not_invoke_broker_sdk(core: EnforcementCore):
    spec, calls = _fake_spec()
    server = BrokerMCPServer(spec, core=core)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="acc1",
            dry_run=True,
            confirmation_token="",
        )
    )
    assert result.ok
    assert result.dry_run
    assert result.idempotency_key
    assert calls == []  # dry-run never reaches the SDK


def test_live_place_without_token_blocked_before_sdk(core: EnforcementCore):
    spec, calls = _fake_spec()
    server = BrokerMCPServer(spec, core=core)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="acc1",
            dry_run=False,
            confirmation_token="",
        )
    )
    assert not result.ok
    assert result.reason == LiveOrderRequiresConfirmation.reason
    assert calls == []  # SDK never reached


def test_live_place_with_valid_token_succeeds(core: EnforcementCore):
    spec, calls = _fake_spec()
    server = BrokerMCPServer(spec, core=core)
    intent = OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=10.0,
        targets=(BrokerAccount(spec.name, "acc1"),),
        price=5.0,
        dry_run=False,
    )
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    leg_token = proposal.legs[0].token
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="acc1",
            dry_run=False,
            confirmation_token=leg_token,
        )
    )
    assert result.ok
    assert result.dry_run is False
    assert calls and calls[0][0] == "trade"
    assert result.idempotency_key == idempotency_key(leg_token, spec.name, "acc1")


def test_health_check_returns_broker_metadata(core: EnforcementCore):
    spec, _ = _fake_spec()
    server = BrokerMCPServer(spec, core=core)
    h = asyncio.run(server.health_check())
    assert h["broker"] == spec.name
    assert h["ok"]
    assert h["breaker_open"] is False


# --------------------------------------------------------------------------
# Account-scoped dispatch guard (final-review C1). Every real registry spec
# is account-blind (`TradeFn` has no account param), so a leg addressed to a
# real (non-"primary") account id must FAIL LOUDLY per leg instead of being
# placed account-blind — for internally-fanning fns (Fennel) blind placement
# multiplies orders (N legs x N-account internal loop).
# --------------------------------------------------------------------------
def _account_blind_spec(name: str = "BlindBroker") -> tuple[BrokerMCPSpec, list[Any]]:
    """Like `_fake_spec` but WITHOUT `account_scoped_trade` — the shape every
    real broker spec has today (build_broker_mcp_spec never sets the flag)."""
    calls: list[Any] = []

    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        calls.append(("trade", side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker: str | None = None) -> Any:
        return {ticker or "ALL": 0.0}

    spec = BrokerMCPSpec(name=name, trade_fn=fake_trade, holdings_fn=fake_holdings)
    return spec, calls


def test_account_blind_spec_rejects_real_account_leg_before_sdk(
    core: EnforcementCore,
):
    spec, calls = _account_blind_spec()
    server = BrokerMCPServer(spec, core=core)
    intent = OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=10.0,
        targets=(BrokerAccount(spec.name, "ira"),),
        price=5.0,
        dry_run=False,
    )
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="ira",
            dry_run=False,
            confirmation_token=proposal.legs[0].token,
        )
    )
    assert not result.ok
    assert result.reason == "account_scoped_dispatch_unsupported"
    assert result.account_id == "ira"
    assert calls == []  # the SDK must never be reached


def test_account_blind_guard_applies_to_dry_run_for_rehearsal_parity(
    core: EnforcementCore,
):
    """Dry-run rehearsals must predict live behavior: a leg that would fail
    the guard live fails it in rehearsal too."""
    spec, calls = _account_blind_spec()
    server = BrokerMCPServer(spec, core=core)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="ira",
            dry_run=True,
            confirmation_token="",
        )
    )
    assert not result.ok
    assert result.reason == "account_scoped_dispatch_unsupported"
    assert calls == []


def test_account_blind_spec_still_places_primary_leg(core: EnforcementCore):
    """The guard must NOT break today's single-account brokers: every
    discovery path assigns the "primary" placeholder, and those legs place
    normally."""
    spec, calls = _account_blind_spec()
    server = BrokerMCPServer(spec, core=core)
    intent = OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=10.0,
        targets=(BrokerAccount(spec.name, "primary"),),
        price=5.0,
        dry_run=False,
    )
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="primary",
            dry_run=False,
            confirmation_token=proposal.legs[0].token,
        )
    )
    assert result.ok
    assert calls and calls[0][0] == "trade"


def test_registry_built_specs_are_account_scoped_only_for_migrated_brokers():
    """Structural pin (supersedes the old "never account-scoped" golden —
    ADR 0006 completion flipped Fennel's trade fn to accept `account_id`):
    `build_broker_mcp_spec` must mark account-scoped ONLY the brokers whose
    trade fn actually accepts the `account_id` kwarg. Fennel is the first
    (and, today, only) migrated broker; the guard's safety for the other 12
    — still account-blind — depends on this staying False for them."""
    migrated = {"Fennel"}
    for broker_name in registry.all_names():
        spec = build_broker_mcp_spec(registry.get(broker_name))
        expected = broker_name in migrated
        assert spec.account_scoped_trade is expected, broker_name


def test_broker_sdk_failure_records_circuit_state(core: EnforcementCore):
    async def boom(side, qty, ticker, price, account_id=None):
        raise RuntimeError("simulated SDK explosion")

    async def empty_holdings(ticker=None):
        return {}

    spec = BrokerMCPSpec(
        name="FlakyBroker",
        trade_fn=boom,
        holdings_fn=empty_holdings,
        # Places at "acc1" — see _fake_spec's note on account_scoped_trade.
        account_scoped_trade=True,
    )
    server = BrokerMCPServer(spec, core=core)
    intent = OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=10.0,
        targets=(BrokerAccount(spec.name, "acc1"),),
        price=5.0,
        dry_run=False,
    )
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    result = asyncio.run(
        server.place_at_broker(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            price=5.0,
            account_id="acc1",
            dry_run=False,
            confirmation_token=proposal.legs[0].token,
        )
    )
    assert not result.ok
    assert result.reason == "broker_sdk_failure"
    state = core.breaker._states[spec.name]  # noqa: SLF001 — test introspection
    assert state.consecutive_errors == 1
