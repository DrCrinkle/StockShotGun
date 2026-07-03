"""F5 v0.2 — legacy `main.py` CLI now routes through the enforcement gate
before `order_processor.process_orders`. These tests exercise the bridge
(`agentic.cli_bridge`) directly with a Router built on fake broker SPECs,
verifying the gate runs and the audit log records both propose + execute
entries. The main.py source itself is statically checked to import the
bridge and call `apply_main_py_gate` on the buy/sell path.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic import cli_bridge
from agentic.router import NullAccountStatusProvider, Router
from enforcement import (
    AuditLog,
    EnforcementCore,
    FrozenTicker,
    PerOrderLimitExceeded,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

ROOT = Path(__file__).resolve().parents[2]


def _fake_spec(name: str) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    return BrokerMCPSpec(name=name, trade_fn=fake_trade, holdings_fn=fake_holdings)


@pytest.fixture
def router(tmp_path: Path) -> Router:
    cli_bridge.reset_router()
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    specs = [_fake_spec("FakeA"), _fake_spec("FakeB")]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    r = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    cli_bridge._router = r  # noqa: SLF001 — test fixture injection
    return r


def test_apply_main_py_gate_mints_proposal(router: Router):
    """Happy path: gate accepts the order, mints a proposal, writes a propose
    audit entry. Returns a dict shaped for main.py's downstream printing."""
    result = asyncio.run(
        cli_bridge.apply_main_py_gate(
            action="buy",
            quantity=2.0,
            ticker="TSLA",
            price=5.0,
            brokers_to_use=["FakeA", "FakeB"],
        )
    )
    assert result["proposal_id"]
    assert result["leg_count"] == 2
    assert result["estimated_usd"] == 2.0 * 5.0 * 2
    # Audit log now has a propose entry
    ok, lines, _ = router.core.audit.verify()
    assert ok and lines >= 1


def test_apply_main_py_gate_rejects_per_order_limit_exceeded(
    router: Router, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SSG_MAX_ORDER_USD", "5")
    with pytest.raises(PerOrderLimitExceeded):
        asyncio.run(
            cli_bridge.apply_main_py_gate(
                action="buy",
                quantity=100.0,
                ticker="TSLA",
                price=5.0,
                brokers_to_use=["FakeA"],
            )
        )


def test_apply_main_py_gate_rejects_frozen_ticker(
    router: Router, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("SSG_FROZEN_TICKERS", "TSLA,GME")
    with pytest.raises(FrozenTicker):
        asyncio.run(
            cli_bridge.apply_main_py_gate(
                action="buy",
                quantity=1.0,
                ticker="TSLA",
                price=5.0,
                brokers_to_use=["FakeA"],
            )
        )


def test_record_main_py_outcome_writes_execute_audit_entry(router: Router):
    """After order_processor returns, the bridge writes one execute entry
    summarizing the per-broker outcome. Audit log stays chain-intact."""
    proposal = asyncio.run(
        cli_bridge.apply_main_py_gate(
            action="buy",
            quantity=2.0,
            ticker="TSLA",
            price=5.0,
            brokers_to_use=["FakeA", "FakeB"],
        )
    )
    asyncio.run(
        cli_bridge.record_main_py_outcome(
            proposal_id=proposal["proposal_id"],
            action="buy",
            quantity=2.0,
            ticker="TSLA",
            price=5.0,
            results={"successful": 2, "failed": 0, "skipped": 0},
        )
    )
    ok, lines, _ = router.core.audit.verify()
    assert ok
    assert lines >= 2  # propose + execute


def test_record_main_py_outcome_marks_partial_on_failures(router: Router):
    proposal = asyncio.run(
        cli_bridge.apply_main_py_gate(
            action="buy",
            quantity=2.0,
            ticker="TSLA",
            price=5.0,
            brokers_to_use=["FakeA", "FakeB"],
        )
    )
    asyncio.run(
        cli_bridge.record_main_py_outcome(
            proposal_id=proposal["proposal_id"],
            action="buy",
            quantity=2.0,
            ticker="TSLA",
            price=5.0,
            results={"successful": 1, "failed": 1, "skipped": 0},
        )
    )
    # Read the last audit line and confirm partial
    import json

    last = router.core.audit.path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(last)
    assert rec["kind"] == "execute"
    assert rec["result"] == "partial"
    assert rec["extra"]["successful"] == 1
    assert rec["extra"]["failed"] == 1


def test_static_main_py_imports_the_gate_bridge():
    """Belt-and-suspenders: confirm the buy/sell handler proposes THEN
    executes through the ExecutionEngine (ADR 0006 Task 3). The buy/sell path
    was repointed from `agentic.cli_bridge` onto `engine.propose_order` +
    `engine.execute_order` — one propose path, one execute path, shared by
    every caller. If a future edit removes the propose step, this test fails
    before the change can ship."""
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    assert "get_engine" in trade_src
    assert "engine.propose_order(" in trade_src
    assert "engine.execute_order(" in trade_src
    # Propose call must appear BEFORE the buy/sell `engine.execute_order(...)`
    # call (ADR 0006 Task 3 replaced `apply_main_py_gate` +
    # `execute_via_router` with `engine.propose_order` + `engine.execute_order`).
    propose_pos = trade_src.find("engine.propose_order(")
    exec_match = re.search(r"engine\.execute_order\(", trade_src)
    assert propose_pos > 0
    assert exec_match is not None
    assert propose_pos < exec_match.start(), (
        "engine.propose_order must run BEFORE engine.execute_order"
    )


def test_static_main_py_buy_sell_path_has_no_unguarded_broker_call():
    """Scoped negative check: in the buy/sell handler body, every block that
    reaches `engine.execute_order` must be preceded by `engine.propose_order`.
    We approximate this by asserting that within the handler, the FIRST
    `engine.execute_order` call is preceded by an `engine.propose_order` call
    in the same function body.

    The buy/sell handler lives in `cli/trade.py::run_trade`; ADR 0006 Task 3
    repointed it onto the ExecutionEngine, so this guard reads that module.
    """
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    run_trade_start = trade_src.find("async def run_trade(")
    assert run_trade_start > 0
    body = trade_src[run_trade_start:]
    op = re.search(r"engine\.execute_order\(", body)
    assert op is not None, "buy/sell engine.execute_order call not found"
    upstream = body[: op.start()]
    assert "engine.propose_order(" in upstream, (
        "buy/sell path must call engine.propose_order BEFORE engine.execute_order"
    )
