from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from typing import Any

import pytest

from agentic._base import (
    BrokerMCPSpec,
    build_fastmcp_server,
)
from enforcement import (
    AuditLog,
    BrokerAccount,
    EnforcementCore,
    OrderIntent,
    OrderSide,
    ProposalStore,
    gate_order,
)
from enforcement.circuit_breaker import CircuitBreaker

EXPECTED_TOOLS = {
    "place_at_broker",
    "get_holdings_at_broker",
    "list_accounts_at_broker",
    "health_check",
}


def _fake_spec() -> BrokerMCPSpec:
    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        return {"ok": True}

    async def fake_holdings(ticker: str | None = None) -> Any:
        return {ticker or "ALL": 0.0}

    return BrokerMCPSpec(
        name="FakeBroker",
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
    )


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


def test_fastmcp_server_registers_canonical_tools(core: EnforcementCore):
    app = build_fastmcp_server(_fake_spec(), core=core)
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_TOOLS, f"unexpected tools: {names}"


def test_each_tool_has_a_documented_schema(core: EnforcementCore):
    app = build_fastmcp_server(_fake_spec(), core=core)
    tools = {t.name: t for t in asyncio.run(app.list_tools())}
    place = tools["place_at_broker"]
    schema = place.inputSchema
    props = schema["properties"]
    required = set(schema["required"])
    # The 6 mandatory args (price is optional with default None)
    assert {"ticker", "qty", "side", "account_id", "dry_run", "confirmation_token"} <= required
    assert "price" in props
    assert props["ticker"]["type"] == "string"


def test_place_at_broker_tool_round_trips_through_fastmcp(core: EnforcementCore):
    """Call the tool function as FastMCP would — through the dict-shaped boundary."""
    spec = _fake_spec()
    app = build_fastmcp_server(spec, core=core)
    intent = OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=10.0,
        targets=(BrokerAccount(spec.name, "acc1"),),
        price=5.0,
        dry_run=False,
    )

    class FakeProvider:
        def get_settled_cash(self, b, a):
            return 10_000.0

        def get_day_trades_in_window(self, b, a):
            return 0

        def get_observed_qty(self, b, a, t):
            return 0.0

    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)

    result = asyncio.run(
        app.call_tool(
            "place_at_broker",
            {
                "ticker": "TSLA",
                "qty": 10.0,
                "side": "buy",
                "account_id": "acc1",
                "dry_run": False,
                "confirmation_token": proposal.legs[0].token,
                "price": 5.0,
            },
        )
    )
    # FastMCP call_tool returns a tuple of (content_blocks, structured?) depending
    # on version — we accept either by hunting for the dict result.
    payload: dict[str, Any] | None = None
    for item in (result if isinstance(result, tuple) else (result,)):
        if isinstance(item, dict) and "ok" in item:
            payload = item
            break
        if isinstance(item, list):
            for block in item:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    try:
                        decoded = json.loads(text)
                        if isinstance(decoded, dict) and "ok" in decoded:
                            payload = decoded
                            break
                    except json.JSONDecodeError:
                        continue
    assert payload is not None, f"could not locate result dict in {result!r}"
    assert payload["ok"] is True
    assert payload["broker"] == spec.name
    assert payload["dry_run"] is False
    assert payload["idempotency_key"]


def test_health_check_tool_returns_metadata(core: EnforcementCore):
    spec = _fake_spec()
    app = build_fastmcp_server(spec, core=core)
    result = asyncio.run(app.call_tool("health_check", {}))
    # The tool returns a dict; FastMCP wraps it — find the dict.
    for item in (result if isinstance(result, tuple) else (result,)):
        if isinstance(item, dict) and item.get("broker") == spec.name:
            return
        if isinstance(item, list):
            for block in item:
                text = getattr(block, "text", None)
                if isinstance(text, str) and spec.name in text:
                    return
    raise AssertionError(f"health_check result missing broker name: {result!r}")


@pytest.mark.parametrize(
    "broker_dir",
    [
        "robinhood",
        "tradier",
        "tastytrade",
        "public",
        "firstrade",
        "fennel",
        "schwab",
        "bbae",
        "dspac",
        "sofi",
        "webull",
        "wellsfargo",
        "chase",
    ],
)
def test_every_real_broker_builds_a_fastmcp_server(broker_dir: str, core: EnforcementCore):
    mod = importlib.import_module(f"agentic.brokers.{broker_dir}")
    app = build_fastmcp_server(mod.SPEC, core=core)
    tools = asyncio.run(app.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS
