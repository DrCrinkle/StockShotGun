"""F5 v0.3 — gate batch helpers + static checks for the four `process_orders`
call sites (main.py buy/sell, main.py from-file batch, main.py automate, TUI
submit_all_orders, TUI retry_timed_out_brokers).
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
    servers = {
        s.name: BrokerMCPServer(s, core=core)
        for s in (_fake_spec("FakeA"), _fake_spec("FakeB"))
    }
    r = Router(
        broker_servers=servers, core=core, provider=NullAccountStatusProvider()
    )
    cli_bridge._router = r  # noqa: SLF001 — test injection
    return r


def test_apply_main_py_gate_batch_mints_one_proposal_per_order(router: Router):
    orders = [
        {"action": "buy", "quantity": 2, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA"]},
        {"action": "buy", "quantity": 1, "ticker": "AAPL",
         "price": 10.0, "selected_brokers": ["FakeB"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    assert len(proposals) == 2
    assert proposals[0]["estimated_usd"] == 2 * 5.0
    assert proposals[1]["estimated_usd"] == 1 * 10.0
    # Each proposal must have a unique id
    assert proposals[0]["proposal_id"] != proposals[1]["proposal_id"]


def test_apply_main_py_gate_batch_aborts_on_first_rejection(
    router: Router, monkeypatch: pytest.MonkeyPatch
):
    """Per design: the first GateError raises and the batch is rejected wholesale."""
    monkeypatch.setenv("SSG_FROZEN_TICKERS", "AAPL")
    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA"]},
        {"action": "buy", "quantity": 1, "ticker": "AAPL",  # frozen
         "price": 5.0, "selected_brokers": ["FakeA"]},
    ]
    with pytest.raises(FrozenTicker):
        asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))


def test_record_main_py_outcome_batch_writes_one_audit_entry_per_order(
    router: Router,
):
    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA"]},
        {"action": "buy", "quantity": 1, "ticker": "AAPL",
         "price": 5.0, "selected_brokers": ["FakeB"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    asyncio.run(
        cli_bridge.record_main_py_outcome_batch(
            proposals=proposals,
            orders=orders,
            results={"successful": 2, "failed": 0, "skipped": 0},
        )
    )
    # 2 propose + 2 execute = 4 entries
    lines = router.core.audit.path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 4
    ok, _, _ = router.core.audit.verify()
    assert ok


def _scan_call_sites(text: str, pattern: str) -> list[int]:
    return [m.start() for m in re.finditer(pattern, text)]


def test_static_main_py_batch_path_is_gated():
    """The from-file batch path's `engine.execute_order` calls MUST be
    preceded by `engine.propose_order` calls in the same function body (ADR
    0006 Task 4 repointed batch.py from the `agentic.cli_bridge` bridge
    functions onto the ExecutionEngine directly — one propose path, one
    execute path, per order)."""
    batch_src = (ROOT / "src" / "cli" / "batch.py").read_text(encoding="utf-8")
    fn_start = batch_src.find("async def _run_batch_from_file(")
    assert fn_start > 0
    fn_end = batch_src.find("\nasync def ", fn_start + 1)
    body = batch_src[fn_start:fn_end if fn_end > 0 else None]
    assert "engine.propose_order(" in body
    assert "engine.execute_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert gate_pos < op_pos


def test_static_main_py_automate_path_is_gated():
    """The automate path's `engine.execute_order` calls MUST be preceded by
    `engine.propose_order` calls (ADR 0006 Task 4 repointed automate.py onto
    the ExecutionEngine directly)."""
    automate_src = (ROOT / "src" / "cli" / "automate.py").read_text(encoding="utf-8")
    fn_start = automate_src.find("async def _run_automate_from_recap(")
    assert fn_start > 0
    fn_end = automate_src.find("\nasync def ", fn_start + 1)
    body = automate_src[fn_start:fn_end if fn_end > 0 else None]
    assert "engine.propose_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert op_pos > 0
    assert gate_pos < op_pos


def test_static_tui_submit_orders_path_is_gated():
    """TUI's submit_all_orders MUST propose (gate) via the ExecutionEngine
    before executing (ADR 0006 Task 5 repointed the TUI off the retired
    `agentic.cli_bridge` batch-gate/router-execute functions onto
    `submit_orders_via_engine`, which calls `engine.propose_order` for
    every order before calling `engine.execute_order` for any of them)."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def submit_all_orders(")
    assert fn_start > 0
    # The next `async def` or `def` at the same indent terminates the function.
    fn_end = re.search(r"\n    (?:async )?def ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "submit_orders_via_engine(" in body


def test_static_tui_retry_path_is_gated():
    """TUI's retry_timed_out_brokers MUST propose (gate) via the
    ExecutionEngine before executing — same helper as submit_all_orders."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def retry_timed_out_brokers(")
    assert fn_start > 0
    fn_end = re.search(r"\n    (?:async )?def ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "submit_orders_via_engine(" in body


def test_static_submit_orders_via_engine_proposes_before_executing():
    """The shared helper itself must propose every order before executing
    any of them — this is where the fail-fast-before-fan-out contract now
    lives (it used to be `apply_main_py_gate_batch` -> `execute_via_router`
    at each call site; ADR 0006 Task 5 centralizes it in one helper)."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def submit_orders_via_engine(")
    assert fn_start > 0
    fn_end = re.search(r"\nasync def |\ndef ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "engine.propose_order(" in body
    assert "engine.execute_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert gate_pos < op_pos


def test_static_no_unguarded_process_orders_call_in_main_or_tui():
    """Belt-and-suspenders: F5 v0.4 removed all `order_processor.process_orders(`
    calls in favor of `execute_via_router(`. Assert every `execute_via_router(`
    has a gate call upstream in its enclosing function (the gate is what
    mints the proposals fed to execute_via_router).
    """
    targets = [
        ROOT / "src" / "main.py",
        ROOT / "src" / "tui" / "app.py",
        *sorted((ROOT / "src" / "cli").glob("*.py")),
    ]
    for path in targets:
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"execute_via_router\(", src):
            preamble = src[: m.start()]
            # Find the enclosing top-level function — `(async )?def ` at any
            # indent shallower-than-or-equal-to the process_orders line. Nested
            # helper `def cli_response_fn(...)` inside the buy/sell handler is
            # at deeper indent and must be skipped.
            #
            # Heuristic: find the indent of the process_orders line itself,
            # then the most-recent function header with indent strictly less
            # than that is the enclosing function.
            line_start = preamble.rfind("\n") + 1
            target_indent = len(src[line_start : m.start()]) - len(
                src[line_start : m.start()].lstrip()
            )
            fn_starts: list[int] = []
            for fnm in re.finditer(r"(?m)^(\s*)(?:async\s+)?def\s+\w+", preamble):
                fn_indent = len(fnm.group(1))
                if fn_indent < target_indent:
                    fn_starts.append(fnm.start())
            assert fn_starts, (
                f"{path}: process_orders at offset {m.start()} has no enclosing function"
            )
            fn_start = fn_starts[-1]
            body_upstream = src[fn_start : m.start()]
            assert (
                "apply_main_py_gate" in body_upstream
            ), (
                f"{path}: process_orders at offset {m.start()} not preceded "
                f"by apply_main_py_gate in enclosing function (starts at "
                f"offset {fn_start})"
            )
