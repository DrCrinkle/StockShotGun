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
    """Belt-and-suspenders: confirm the buy/sell handler imports the gate
    bridge AND calls `apply_main_py_gate` before execution. The buy/sell path
    was extracted from main.run_cli into cli/trade.py during the main.py split,
    so this guard now reads that module. If a future edit removes the gate,
    this test fails before the change can ship."""
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    assert "from agentic.cli_bridge import" in trade_src
    assert "apply_main_py_gate" in trade_src
    assert "record_main_py_outcome" in trade_src
    # Gate call must appear BEFORE the buy/sell `execute_via_router(...)` call
    # (F5 v0.4 replaced `order_processor.process_orders([order]` with
    # `execute_via_router(proposals=[gate_proposal], ...)`)
    gate_pos = trade_src.find("apply_main_py_gate(")
    op_match = re.search(
        r"execute_via_router\(\s*proposals=\[gate_proposal\]", trade_src
    )
    assert gate_pos > 0
    assert op_match is not None
    assert gate_pos < op_match.start(), (
        "apply_main_py_gate must run BEFORE execute_via_router"
    )


def test_static_main_py_buy_sell_path_has_no_unguarded_broker_call():
    """Scoped negative check: in the buy/sell handler body, every block that
    reaches `execute_via_router` must be preceded by `apply_main_py_gate`. We
    approximate this by asserting that within the handler, the FIRST
    `execute_via_router` call is preceded by an `apply_main_py_gate` call in
    the same function body.

    The buy/sell handler was extracted from main.run_cli into
    `cli/trade.py::run_trade` during the main.py split, so this guard now reads
    that module and locates `async def run_trade(`.
    """
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    run_trade_start = trade_src.find("async def run_trade(")
    assert run_trade_start > 0
    body = trade_src[run_trade_start:]
    # F5 v0.4 replaced process_orders([order] with execute_via_router(proposals=[gate_proposal])
    op = re.search(
        r"execute_via_router\(\s*proposals=\[gate_proposal\]", body
    )
    assert op is not None, "buy/sell execute_via_router call not found"
    upstream = body[: op.start()]
    assert "apply_main_py_gate(" in upstream, (
        "buy/sell path must call apply_main_py_gate BEFORE execute_via_router"
    )
