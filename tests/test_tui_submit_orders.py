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


def _failed_leg_execution(
    ticker: str,
    side: str,
    brokers: list[str],
    *,
    reason: str | None = "broker_error",
    detail: str | None = "insufficient funds",
) -> dict[str, Any]:
    """A NON-rejected execution whose individual legs came back ``ok=False``.

    Distinct from ``_rejected_execution`` (gate refused the whole order before
    any leg dispatched, ``results=[]``): here the order was executed and each
    broker leg failed on its own, carrying a per-leg ``reason``/``detail``.
    This is the shape the per-leg failure diagnostics (Fix 2a) read from.
    """
    return {
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "dry_run": False,
        "results": [
            {
                "broker": b,
                "account_id": "primary",
                "ok": False,
                "dry_run": False,
                "idempotency_key": f"idem-{b}",
                "reason": reason,
                "detail": detail,
            }
            for b in brokers
        ],
        "success_count": 0,
        "failure_count": len(brokers),
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


def test_rejected_first_then_success_continues_execution():
    """Regression pin: a rejected order EARLIER in the batch must NOT abort
    execution of orders that follow it. The rejected order is placed FIRST so
    that a `break`-instead-of-`continue` regression in the execute loop is
    caught — with the rejection last (as in the mixed-batch test above) an
    early exit would leave nothing unexecuted to prove the loop kept going.

    Uses the engine's own `calls` recorder as a call-recording execution stub:
    we assert the SECOND (successful) order's `execute_order` actually ran, not
    merely that its status appears in the aggregate.
    """
    orders = [
        _order("TSLA", ["Robinhood"], action="sell"),  # rejected, FIRST
        _order("AAPL", ["Public", "Fennel"]),           # ok, must still run
    ]
    engine = _FakeEngine(
        executions={
            "TSLA": _rejected_execution(reason="breaker_open", detail="circuit open"),
            "AAPL": _ok_execution("AAPL", "buy", ["Public", "Fennel"]),
        }
    )
    messages: list[str] = []

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append)
    )

    # The successful order's execution actually ran despite the earlier
    # rejection — call-recording proof, not just a status snapshot.
    assert "execute:prop-TSLA-1" in engine.calls
    assert "execute:prop-AAPL-2" in engine.calls

    # Both the rejected and the successful order's statuses are present.
    assert len(results["statuses"]) == 2
    tsla_status = next(s for s in results["statuses"] if s["ticker"] == "TSLA")
    aapl_status = next(s for s in results["statuses"] if s["ticker"] == "AAPL")
    assert sorted(tsla_status["failed"]) == ["Robinhood"]
    assert tsla_status["successful"] == []
    assert sorted(aapl_status["successful"]) == ["Fennel", "Public"]

    # Aggregation reflects BOTH orders: 2 succeeded (AAPL legs) + 1 failed
    # (TSLA's single rejected broker).
    assert results["successful"] == 2
    assert results["failed"] == 1


def test_progress_fn_raising_never_aborts_batch():
    """Fix 2b: UI progress callbacks are best-effort — a `progress_fn` that
    throws on EVERY call must never abort order execution. Every order in the
    batch must still execute and the aggregate result must be complete.
    """
    orders = [
        _order("TSLA", ["Robinhood"]),
        _order("AAPL", ["Public", "Fennel"]),
    ]
    engine = _FakeEngine(
        executions={
            "TSLA": _ok_execution("TSLA", "buy", ["Robinhood"]),
            "AAPL": _ok_execution("AAPL", "buy", ["Public", "Fennel"]),
        }
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("UI callback exploded")

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=_boom)
    )

    # Every order executed despite the throwing callback (call-recording proof).
    assert "execute:prop-TSLA-1" in engine.calls
    assert "execute:prop-AAPL-2" in engine.calls

    # Aggregate is complete: all expected statuses present, counts correct.
    assert results["successful"] == 3
    assert results["failed"] == 0
    assert results["skipped"] == 0
    assert len(results["statuses"]) == 2
    assert sorted(results["statuses"][0]["successful"]) == ["Robinhood"]
    assert sorted(results["statuses"][1]["successful"]) == ["Fennel", "Public"]


def test_per_leg_failure_message_includes_reason_and_detail():
    """Fix 2a: per-leg failure messages restore the old bridge's
    `✗ {broker}: {reason} - {detail}` diagnostics instead of a bare `failed`.
    """
    orders = [_order("TSLA", ["Robinhood"])]
    engine = _FakeEngine(
        executions={
            "TSLA": _failed_leg_execution(
                "TSLA", "buy", ["Robinhood"],
                reason="broker_error", detail="insufficient funds",
            )
        }
    )
    messages: list[str] = []

    results = _run(
        submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append)
    )

    assert results["failed"] == 1
    assert any(
        "Robinhood: broker_error - insufficient funds" in m for m in messages
    )


def test_tui_submit_initializes_sessions_before_validation():
    """Final-review I2: `submit_all_orders` must initialize broker sessions
    BEFORE `validate_targets`/`propose_order`, like every CLI path — engine
    account discovery reads `session_manager.sessions` cold, so skipping the
    init step makes discovery silently differ between the first and later
    submissions. `submit_all_orders` is a closure inside `run_tui` (no
    seam to call it headlessly), so this is a static-source pin — the same
    technique tests/agentic/test_f5_v04_router_execution.py uses.
    """
    import inspect
    import re

    import tui.app as app_mod

    src = inspect.getsource(app_mod.run_tui)
    submit_match = re.search(r"async def submit_all_orders\(", src)
    assert submit_match, "submit_all_orders not found in run_tui"
    # Slice out just the submit_all_orders body (up to the next def at the
    # same nesting depth).
    rest = src[submit_match.start():]
    next_def = re.search(r"\n    (?:async )?def (?!submit_all_orders)", rest)
    body = rest[: next_def.start()] if next_def else rest
    init_pos = body.find("initialize_selected_sessions(")
    validate_pos = body.find("validate_targets(")
    assert init_pos != -1, "submit_all_orders must initialize broker sessions"
    assert validate_pos != -1
    assert init_pos < validate_pos, (
        "session init must run BEFORE validate_targets/propose_order"
    )


def test_tui_retry_initializes_sessions_before_validation():
    """Same I2 pin for `retry_timed_out_brokers`: a broker that timed out
    during the original submission likely has no usable session."""
    import inspect
    import re

    import tui.app as app_mod

    src = inspect.getsource(app_mod.run_tui)
    retry_match = re.search(r"async def retry_timed_out_brokers\(", src)
    assert retry_match, "retry_timed_out_brokers not found in run_tui"
    rest = src[retry_match.start():]
    next_def = re.search(r"\n    (?:async )?def (?!retry_timed_out_brokers)", rest)
    body = rest[: next_def.start()] if next_def else rest
    init_pos = body.find("initialize_selected_sessions(")
    validate_pos = body.find("validate_targets(")
    assert init_pos != -1
    assert validate_pos != -1
    assert init_pos < validate_pos


def test_per_leg_failure_message_falls_back_to_failed_when_no_reason():
    """Fix 2a fallback: when a failed leg carries neither reason nor detail,
    the message degrades gracefully to `✗ {broker}: failed` — no dangling
    `- ` and no `None` text.
    """
    orders = [_order("TSLA", ["Robinhood"])]
    engine = _FakeEngine(
        executions={
            "TSLA": _failed_leg_execution(
                "TSLA", "buy", ["Robinhood"], reason=None, detail=None,
            )
        }
    )
    messages: list[str] = []

    _run(submit_orders_via_engine(orders, engine=engine, progress_fn=messages.append))

    failure_msgs = [m for m in messages if "Robinhood" in m and "✗" in m]
    assert failure_msgs, "expected a per-leg failure message for Robinhood"
    assert any(m.rstrip().endswith("Robinhood: failed") for m in failure_msgs)
    for m in failure_msgs:
        assert "None" not in m
        assert not m.rstrip().endswith("-")
