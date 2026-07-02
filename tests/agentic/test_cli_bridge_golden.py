"""ADR-0006 step-0 characterization ("golden") test for the cli_bridge gate.

This pins the EXACT divergence ADR 0006 calls out: the main CLI / TUI propose
path (`agentic.cli_bridge.apply_main_py_gate`) builds an intent that

  1. targets ONE synthetic ``BrokerAccount(broker, "primary")`` per broker —
     bypassing the per-account fan-out that `Router.propose_order` performs via
     `_discover_accounts` (ADR 0001 / ADR 0006 Context #1), and
  2. is always ``dry_run=False`` regardless of caller intent (ADR 0006 Context #2).

>>> EXPECTED TO CHANGE WHEN ADR 0006 LANDS <<<
When the CLI is repointed at `ExecutionEngine.propose_order`, `targets` becomes
the discovered accounts and `dry_run` is bound from the caller. At that point this
test should be rewritten (or deleted with `cli_bridge`) — a failure here after the
redesign is the intended signal, not a regression.

We intercept `gate_order` (which `apply_main_py_gate` calls) to capture the
`OrderIntent` it is handed, and stub `get_router` so no real 13-broker discovery
or enforcement runs.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agentic.cli_bridge as bridge
from enforcement import BrokerAccount, OrderIntent, OrderSide


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def capture_gate(monkeypatch):
    """Patch cli_bridge so apply_main_py_gate runs but the gate is a spy."""
    captured: dict = {}

    async def fake_get_router():
        # provider is a plain namespace (NOT BrokerServerAccountStatusProvider),
        # so apply_main_py_gate skips the prefetch branch.
        return SimpleNamespace(core=object(), provider=SimpleNamespace())

    def fake_gate_order(core, intent, provider, *, ref_price):
        captured["intent"] = intent
        captured["ref_price"] = ref_price
        proposal = SimpleNamespace(
            proposal_id="prop-1",
            estimated_usd=12.5,
            leg_count=len(intent.targets),
        )
        decision = SimpleNamespace(skipped_brokers=[])
        return proposal, decision

    monkeypatch.setattr(bridge, "get_router", fake_get_router)
    monkeypatch.setattr(bridge, "gate_order", fake_gate_order)
    return captured


def test_gate_intent_uses_one_primary_leg_per_broker(capture_gate):
    out = _run(
        bridge.apply_main_py_gate(
            action="buy",
            quantity=3,
            ticker="XYZ",
            price=None,
            brokers_to_use=["Public", "Robinhood"],
        )
    )

    intent: OrderIntent = capture_gate["intent"]
    # ADR 0006 #1 — one synthetic "primary" leg per broker, no account discovery.
    assert intent.targets == (
        BrokerAccount("Public", "primary"),
        BrokerAccount("Robinhood", "primary"),
    )
    # ADR 0006 #2 — proposal is born live regardless of any dry-run intent.
    assert intent.dry_run is False
    assert intent.side == OrderSide("buy")
    assert intent.qty == 3
    assert intent.ticker == "XYZ"
    assert intent.price is None

    # Returned summary shape the CLI/TUI depend on today.
    assert out == {
        "proposal_id": "prop-1",
        "estimated_usd": 12.5,
        "leg_count": 2,
        "skipped_brokers": [],
    }


def test_gate_ref_price_defaults_to_zero_for_market_orders(capture_gate):
    _run(
        bridge.apply_main_py_gate(
            action="sell",
            quantity=1,
            ticker="ABC",
            price=None,
            brokers_to_use=["Public"],
        )
    )
    # Market order (no price) → ref_price 0.0 is the current contract.
    assert capture_gate["ref_price"] == 0.0
    assert capture_gate["intent"].side == OrderSide("sell")
