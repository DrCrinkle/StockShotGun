"""ADR 0005 — the Proposal is self-describing; the router-side intent cache is gone.

These tests pin the two behaviors that the cache deletion makes true:

1. execute works against a FRESH Router that never saw the propose call — proving
   the params come from the durable ProposalStore, not in-process memory (the
   old `_router_intent_cache` would have made this a cache miss).
2. execute enforces that the caller's dry_run matches the proposal's minted
   dry_run, rather than silently flipping the order's mode.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.router import NullAccountStatusProvider, Router
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker


def _fake_broker(name: str, trade_log: list[Any]) -> BrokerMCPSpec:
    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        trade_log.append((name, side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker: str | None = None) -> Any:
        return {ticker or "ALL": 100.0}

    return BrokerMCPSpec(name=name, trade_fn=fake_trade, holdings_fn=fake_holdings)


def _make_core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


def _router(core: EnforcementCore, log: list[Any]) -> Router:
    specs = [_fake_broker("FakeA", log), _fake_broker("FakeB", log)]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    return Router(broker_servers=servers, core=core, provider=NullAccountStatusProvider())


def test_execute_works_on_fresh_router_with_no_in_memory_state(tmp_path: Path):
    """Propose on one Router, execute on a DIFFERENT Router that shares only the
    durable store. The old intent cache would have missed; the self-describing
    Proposal makes it work."""
    core = _make_core(tmp_path)
    log_a: list[Any] = []
    proposer = _router(core, log_a)
    proposal = asyncio.run(
        proposer.propose_order(
            ticker="TSLA", qty=2.0, side="buy",
            brokers=["FakeA", "FakeB"], price=5.0, dry_run=False,
        )
    )

    # A brand-new Router instance — no shared in-memory cache, only the store.
    log_b: list[Any] = []
    executor = _router(core, log_b)
    result = asyncio.run(
        executor.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )

    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert {entry[0] for entry in log_b} == {"FakeA", "FakeB"}
    # The order params were recovered from the store, not re-supplied.
    assert result["ticker"] == "TSLA"
    assert result["qty"] == 2.0
    assert result["side"] == "buy"


def test_execute_dry_run_mismatch_is_rejected(tmp_path: Path):
    """A proposal minted dry_run=True cannot be executed live (and vice versa);
    execute rejects the mismatch explicitly instead of failing every leg with an
    opaque intent_mismatch."""
    core = _make_core(tmp_path)
    log: list[Any] = []
    router = _router(core, log)
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA", qty=1.0, side="buy",
            brokers=["FakeA"], price=5.0, dry_run=True,
        )
    )
    result = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    assert result["rejected"] is True
    assert result["reason"] == "dry_run_mismatch"
    assert log == []  # never reached the broker
