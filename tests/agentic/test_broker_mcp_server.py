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
    """Build a spec whose trade_fn records calls instead of hitting an SDK."""
    calls: list[Any] = []

    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        calls.append(("trade", side, qty, ticker, price))
        return {"ok": True, "fill_qty": qty, "fill_price": price or 0.0}

    async def fake_holdings(ticker: str | None = None) -> Any:
        calls.append(("holdings", ticker))
        return {ticker or "ALL": 0.0}

    spec = BrokerMCPSpec(
        name=name,
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
        validate_fn=None,
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


def test_broker_sdk_failure_records_circuit_state(core: EnforcementCore):
    async def boom(side, qty, ticker, price):
        raise RuntimeError("simulated SDK explosion")

    async def empty_holdings(ticker=None):
        return {}

    spec = BrokerMCPSpec(
        name="FlakyBroker",
        trade_fn=boom,
        holdings_fn=empty_holdings,
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
