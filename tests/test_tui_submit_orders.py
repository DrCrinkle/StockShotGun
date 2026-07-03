"""Tests for `tui.app.submit_orders_via_engine` — the ADR 0006 Task 5 helper
that repoints the TUI's order submission onto the ExecutionEngine.

The TUI is interactive: unlike the CLI/batch/automate paths (which raise
`CliRuntimeError` and abort the process on an execute-time rejection), this
helper must never raise on a rejected execution — it maps `rejected=True`
to the same all-failed outcome the retired `agentic.cli_bridge.
execute_via_router` produced (mark every selected broker as failed and keep
going), so the TUI's response panel and broker-status widgets can display it
without crashing the event loop.

A propose-time `GateError` is the one exception that DOES propagate — the
TUI's call sites already have an `except GateError` around this helper that
reproduces the old `apply_main_py_gate_batch` rejection display (clear the
queue, show the reason). Two-phase propose-then-execute ordering (propose
every order before executing any of them) is pinned here too.

Harness mirrors tests/test_cli_batch_golden.py: a stub engine records
propose/execute calls and returns canned results; run the coroutine with
`asyncio.run` from a sync test function (this repo has no pytest-asyncio
mark wiring — see other `tests/test_cli_*_golden.py` files for precedent).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from enforcement import GateError
from tui.app import submit_orders_via_engine


def _run(coro):
    return asyncio.run(coro)


class _FakeEngine:
    """Minimal stand-in for ExecutionEngine.propose_order/execute_order.

    Records call order so tests can assert propose-all-then-execute-all
    ordering (not interleaved per order).
    """

    def __init__(
        self,
        *,
        executions: dict[str, dict[str, Any]] | None = None,
        gate_error_on_ticker: str | None = None,
    ) -> None:
        self.executions = executions or {}
        self.gate_error_on_ticker = gate_error_on_ticker
        self.calls: list[str] = []
        self._next_id = 0

    async def propose_order(self, *, ticker, qty, side, brokers, price, dry_run):
        self.calls.append(f"propose:{ticker}")
        if ticker == self.gate_error_on_ticker:
            raise GateError(f"rejected {ticker}")
        self._next_id += 1
        proposal_id = f"prop-{ticker}-{self._next_id}"
        return {
            "proposal_id": proposal_id,
            "leg_count": len(brokers),
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "dry_run": dry_run,
        }

    async def execute_order(self, *, proposal_id, dry_run):
        self.calls.append(f"execute:{proposal_id}")
        ticker = proposal_id.split("-")[1]
        return self.executions[ticker]


def _ok_execution(ticker: str, side: str, brokers: list[str]) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "dry_run": False,
        "results": [
            {
                "broker": b,
                "account_id": "primary",
                "ok": True,
                "dry_run": False,
                "idempotency_key": f"idem-{b}",
                "reason": None,
                "detail": "placed",
            }
            for b in brokers
        ],
        "success_count": len(brokers),
        "failure_count": 0,
    }


def _rejected_execution(reason: str = "breaker_open", detail: str = "circuit open") -> dict[str, Any]:
    return {
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": reason,
        "detail": detail,
    }


def _order(ticker: str, brokers: list[str], action: str = "buy") -> dict[str, Any]:
    return {
        "action": action,
        "quantity": 1,
        "ticker": ticker,
        "price": None,
        "selected_brokers": list(brokers),
    }


def test_two_phase_ordering_proposes_all_before_executing_any():
    orders = [_order("TSLA", ["Robinhood"]), _order("AAPL", ["Public"])]
    engine = _FakeEngine(
        executions={
            "TSLA": _ok_execution("TSLA", "buy", ["Robinhood"]),
            "AAPL": _ok_execution("AAPL", "buy", ["Public"]),
        }
    )
    messages: list[str] = []

    _run(submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append))

    # Both proposes happen before either execute — propose-all-then-
    # execute-all, not interleaved per order.
    propose_indexes = [i for i, c in enumerate(engine.calls) if c.startswith("propose:")]
    execute_indexes = [i for i, c in enumerate(engine.calls) if c.startswith("execute:")]
    assert max(propose_indexes) < min(execute_indexes)
    assert engine.calls == [
        "propose:TSLA",
        "propose:AAPL",
        "execute:prop-TSLA-1",
        "execute:prop-AAPL-2",
    ]


def test_successful_execution_renders_and_aggregates():
    orders = [_order("TSLA", ["Robinhood", "Public"])]
    engine = _FakeEngine(
        executions={"TSLA": _ok_execution("TSLA", "buy", ["Robinhood", "Public"])}
    )
    messages: list[str] = []

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append)
    )

    assert results["successful"] == 2
    assert results["failed"] == 0
    assert results["skipped"] == 0
    assert len(results["statuses"]) == 1
    assert set(results["statuses"][0]["successful"]) == {"Robinhood", "Public"}
    assert any("Robinhood: placed" in m for m in messages)
    assert any("Public: placed" in m for m in messages)


def test_gate_error_on_propose_propagates_without_executing():
    """A propose-time GateError must propagate to the caller (the TUI's
    `except GateError` around this call reproduces the old
    `apply_main_py_gate_batch` rejection display) — and must not execute
    anything, mirroring the old fail-fast-before-fan-out contract."""
    orders = [_order("TSLA", ["Robinhood"]), _order("AAPL", ["Public"])]
    engine = _FakeEngine(
        executions={"AAPL": _ok_execution("AAPL", "buy", ["Public"])},
        gate_error_on_ticker="TSLA",
    )
    messages: list[str] = []

    with pytest.raises(GateError):
        _run(submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append))

    assert not any(c.startswith("execute:") for c in engine.calls)


def test_rejected_execution_does_not_raise_and_marks_all_brokers_failed():
    """The core TUI-specific contract: unlike the CLI/batch/automate paths
    (which raise CliRuntimeError on execute-time rejection), the TUI helper
    must swallow the rejection and render it as an all-failed outcome for
    that order's selected brokers — reproducing what the retired
    `execute_via_router` did when `result.get("rejected")` was true."""
    orders = [_order("TSLA", ["Robinhood", "Public"], action="sell")]
    engine = _FakeEngine(
        executions={"TSLA": _rejected_execution(reason="breaker_open", detail="circuit open")}
    )
    messages: list[str] = []

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append)
    )

    assert results["successful"] == 0
    assert results["failed"] == 2
    assert results["skipped"] == 0
    status = results["statuses"][0]
    assert sorted(status["failed"]) == ["Public", "Robinhood"]
    assert status["successful"] == []
    assert status["reason"] == "breaker_open"
    assert status["detail"] == "circuit open"
    assert any("rejected by enforcement gate" in m for m in messages)


def test_mixed_batch_one_ok_one_rejected_aggregates_correctly():
    orders = [
        _order("TSLA", ["Robinhood"]),
        _order("AAPL", ["Public", "Fennel"]),
    ]
    engine = _FakeEngine(
        executions={
            "TSLA": _ok_execution("TSLA", "buy", ["Robinhood"]),
            "AAPL": _rejected_execution(reason="freeze_list"),
        }
    )
    messages: list[str] = []

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append)
    )

    assert results["successful"] == 1
    assert results["failed"] == 2
    assert len(results["statuses"]) == 2
    assert results["statuses"][0]["successful"] == ["Robinhood"]
    assert sorted(results["statuses"][1]["failed"]) == ["Fennel", "Public"]
