"""ADR-0006 step-5 golden test for `ExecutionEngine.propose_order` — the one
propose path every caller (CLI, TUI, operator CLI, MCP server) now shares.

This is the INVERSE of the old `cli_bridge.apply_main_py_gate` characterization
test (deleted with `cli_bridge` itself, ADR 0006 step 5): that test pinned the
bridge's divergence from the ideal — one synthetic ``BrokerAccount(broker,
"primary")`` leg per broker, `dry_run` always `False` regardless of caller
intent. Now that every caller proposes through `ExecutionEngine.propose_order`,
this test pins the corrected behavior:

  1. `targets` on the built `OrderIntent` carries the REAL discovered accounts
     per broker (via `_discover_accounts` -> `list_accounts_at_broker`), not a
     single synthetic "primary" leg — including brokers that report more than
     one account (ADR 0001 multi-account fan-out).
  2. `dry_run` is bound from the caller's `propose_order(dry_run=...)` argument,
     not hardcoded.

Same capture technique as the old bridge test: `propose_order` calls
`gate_order` internally, so intercepting `gate_order` captures the exact
`OrderIntent` the engine built without needing to run real enforcement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.router import NullAccountStatusProvider, Router
from enforcement import AuditLog, BrokerAccount, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker


def _run(coro):
    return asyncio.run(coro)


def _fake_spec(name: str, account_ids: list[str]) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    async def fake_list_accounts():
        return account_ids

    return BrokerMCPSpec(
        name=name,
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
        list_accounts_fn=fake_list_accounts,
    )


@pytest.fixture
def engine(tmp_path: Path) -> Router:
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    # "Public" reports a single account; "Robinhood" reports TWO — proving
    # propose_order discovers real per-broker accounts instead of minting one
    # synthetic "primary" leg per broker (the old bridge's behavior).
    specs = [
        _fake_spec("Public", ["primary"]),
        _fake_spec("Robinhood", ["taxable", "ira"]),
    ]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    return Router(broker_servers=servers, core=core, provider=NullAccountStatusProvider())


@pytest.fixture
def capture_gate(monkeypatch, engine: Router):
    """Patch `gate_order` inside the router module so `propose_order` runs the
    real account-discovery + intent-building path but the gate itself is a
    spy — same technique the old cli_bridge golden test used."""
    captured: dict = {}

    def fake_gate_order(core, intent, provider, *, ref_price):
        captured["intent"] = intent
        captured["ref_price"] = ref_price
        proposal = SimpleNamespace(
            proposal_id="prop-1",
            valid_until_ts=0.0,
            estimated_usd=12.5,
            leg_count=len(intent.targets),
        )
        decision = SimpleNamespace(skipped_brokers=[])
        return proposal, decision

    import agentic.router._server as server_mod

    monkeypatch.setattr(server_mod, "gate_order", fake_gate_order)
    return captured


def test_propose_order_intent_uses_real_discovered_accounts(
    engine: Router, capture_gate: dict
):
    out = _run(
        engine.propose_order(
            ticker="XYZ",
            qty=3,
            side="buy",
            brokers=["Public", "Robinhood"],
            price=None,
            dry_run=False,
        )
    )

    intent = capture_gate["intent"]
    # Real discovered accounts — Robinhood's two accounts BOTH show up as
    # targets, unlike the old bridge's one-synthetic-"primary"-leg-per-broker.
    assert set(intent.targets) == {
        BrokerAccount("Public", "primary"),
        BrokerAccount("Robinhood", "taxable"),
        BrokerAccount("Robinhood", "ira"),
    }
    assert intent.side.value == "buy"
    assert intent.qty == 3
    assert intent.ticker == "XYZ"
    assert intent.price is None

    assert out["proposal_id"] == "prop-1"
    assert out["estimated_usd"] == 12.5
    assert out["leg_count"] == 3


def test_propose_order_binds_dry_run_from_caller(engine: Router, capture_gate: dict):
    _run(
        engine.propose_order(
            ticker="XYZ",
            qty=1,
            side="buy",
            brokers=["Public"],
            price=None,
            dry_run=True,
        )
    )
    # ADR 0006 fix: dry_run is bound from the caller's propose_order argument,
    # not hardcoded False as the retired cli_bridge did.
    assert capture_gate["intent"].dry_run is True


def test_propose_order_ref_price_defaults_to_zero_for_market_orders(
    engine: Router, capture_gate: dict
):
    _run(
        engine.propose_order(
            ticker="ABC",
            qty=1,
            side="sell",
            brokers=["Public"],
            price=None,
            dry_run=False,
        )
    )
    assert capture_gate["ref_price"] == 0.0
    assert capture_gate["intent"].side.value == "sell"
