"""ADR-0006 Task 4 golden tests for the `--from-file` batch path
(`cli.batch._run_batch_from_file`).

These pin the *new* observable behavior once batch.py is repointed from the
`agentic.cli_bridge` (apply_main_py_gate_batch / execute_via_router /
record_main_py_outcome_batch) onto the ExecutionEngine — one propose call and
one execute call per order, looped, fail-fast on the first GateError (mirrors
`apply_main_py_gate_batch`'s aggregate semantics), with a rejection at execute
time raising CliRuntimeError(FULL_BROKER_FAILURE) rather than rendering as a
silent success.

Harness mirrors tests/test_cli_trade_golden.py: a stub engine records
propose/execute calls and returns canned results; run the handler in-process
with SimpleNamespace args/context.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import cli.batch as batch_mod
from cli.batch import _run_batch_from_file
from cli_runtime import ExitCode
from enforcement import GateError


def _run(coro):
    return asyncio.run(coro)


async def _some_trade(*a):
    return True


@pytest.fixture
def two_brokers(monkeypatch):
    monkeypatch.setattr(
        batch_mod,
        "BROKER_FUNCTIONS",
        {
            "Public": {"trade": _some_trade},
            "Robinhood": {"trade": _some_trade},
        },
    )


@pytest.fixture
def stub_session_init(monkeypatch):
    async def fake_init(brokers):
        return None

    monkeypatch.setattr(
        batch_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )


def _ctx(**over):
    base = dict(output_format="json", mock_brokers=False, dry_run=False)
    base.update(over)
    return SimpleNamespace(**base)


class _StubEngine:
    """Records propose/execute calls; returns per-order canned proposals /
    executions keyed by call order. `propose_side_effects` may hold GateError
    instances to simulate a mid-batch rejection."""

    def __init__(self, proposals=None, executions=None, propose_side_effects=None):
        self.propose_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self._proposals = list(proposals or [])
        self._executions = list(executions or [])
        self._propose_side_effects = list(propose_side_effects or [])
        self._propose_idx = 0
        self._execute_idx = 0

    async def validate_targets(self, **kwargs):
        return list(kwargs["selected_brokers"]), []

    async def propose_order(self, **kwargs):
        self.propose_calls.append(kwargs)
        idx = self._propose_idx
        self._propose_idx += 1
        if idx < len(self._propose_side_effects) and self._propose_side_effects[idx] is not None:
            raise self._propose_side_effects[idx]
        return self._proposals[idx]

    async def execute_order(self, **kwargs):
        self.execute_calls.append(kwargs)
        idx = self._execute_idx
        self._execute_idx += 1
        return self._executions[idx]


def _proposal(pid: str, leg_count: int = 1) -> dict:
    return {
        "proposal_id": pid,
        "estimated_usd": 1.0,
        "leg_count": leg_count,
        "accounts_by_broker": {"Public": ["primary"]},
        "skipped_brokers": [],
    }


def _execution(pid: str, ticker: str, side: str, broker: str = "Public", ok: bool = True) -> dict:
    return {
        "proposal_id": pid,
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "dry_run": False,
        "results": [
            {
                "broker": broker,
                "account_id": "primary",
                "ok": ok,
                "dry_run": False,
                "idempotency_key": f"k-{pid}",
                "reason": None if ok else "boom",
                "detail": "placed" if ok else "broker error",
            }
        ],
        "success_count": 1 if ok else 0,
        "failure_count": 0 if ok else 1,
    }


def _write_batch_file(tmp_path, orders):
    path = tmp_path / "batch.json"
    path.write_text(json.dumps({"orders": orders}), encoding="utf-8")
    return str(path)


# --------------------------------------------------------------------------
# Engine wiring — propose then execute, per order, aggregated.
# --------------------------------------------------------------------------
def test_batch_proposes_and_executes_each_order_on_the_engine(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    engine = _StubEngine(
        proposals=[_proposal("p1"), _proposal("p2")],
        executions=[
            _execution("p1", "TSLA", "buy"),
            _execution("p2", "AAPL", "sell"),
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [
            {"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]},
            {"action": "sell", "quantity": 1, "ticker": "AAPL", "brokers": ["Public"]},
        ],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    exit_code, data = _run(_run_batch_from_file(args, parser=None, context=_ctx()))

    assert len(engine.propose_calls) == 2
    assert len(engine.execute_calls) == 2
    assert engine.propose_calls[0]["dry_run"] is False
    assert engine.execute_calls[0]["dry_run"] is False
    assert engine.execute_calls[0]["proposal_id"] == "p1"
    assert engine.execute_calls[1]["proposal_id"] == "p2"

    assert exit_code == ExitCode.SUCCESS
    assert data["results"]["successful"] == 2
    assert data["results"]["failed"] == 0
    assert len(data["results"]["statuses"]) == 2


# --------------------------------------------------------------------------
# Fail-fast on first GateError — preserves apply_main_py_gate_batch semantics.
# --------------------------------------------------------------------------
def test_batch_aborts_wholesale_on_first_gate_rejection(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    gate_err = GateError("frozen ticker")
    gate_err.reason = "frozen_ticker"  # type: ignore[attr-defined]
    engine = _StubEngine(
        proposals=[_proposal("p1")],
        executions=[_execution("p1", "TSLA", "buy")],
        propose_side_effects=[None, gate_err],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(batch_mod, "gate_error_to_exit_code", lambda e: 2)

    from_file = _write_batch_file(
        tmp_path,
        [
            {"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]},
            {"action": "buy", "quantity": 1, "ticker": "AAPL", "brokers": ["Public"]},  # rejected
        ],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    with pytest.raises(batch_mod.CliRuntimeError) as excinfo:
        _run(_run_batch_from_file(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.INVALID_ARGS
    # Second order's proposal was attempted (that's what raised); no execute
    # call happened for either order — the whole batch is aborted before any
    # fan-out, same as apply_main_py_gate_batch's first-rejection contract.
    assert len(engine.propose_calls) == 2
    assert len(engine.execute_calls) == 0


# --------------------------------------------------------------------------
# Execute-time rejection must NOT read as success.
# --------------------------------------------------------------------------
def test_batch_execute_rejection_raises_full_broker_failure(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    rejection = {
        "proposal_id": "p1",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id p1",
    }
    engine = _StubEngine(
        proposals=[_proposal("p1")],
        executions=[rejection],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [{"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]}],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    with pytest.raises(batch_mod.CliRuntimeError) as excinfo:
        _run(_run_batch_from_file(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.FULL_BROKER_FAILURE
    assert excinfo.value.exit_code != ExitCode.SUCCESS


# --------------------------------------------------------------------------
# Partial failure aggregation across orders via aggregate_execution_results.
# --------------------------------------------------------------------------
def test_batch_aggregates_partial_failures_across_orders(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    engine = _StubEngine(
        proposals=[_proposal("p1"), _proposal("p2")],
        executions=[
            _execution("p1", "TSLA", "buy", ok=True),
            _execution("p2", "AAPL", "sell", ok=False),
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [
            {"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]},
            {"action": "sell", "quantity": 1, "ticker": "AAPL", "brokers": ["Public"]},
        ],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    exit_code, data = _run(_run_batch_from_file(args, parser=None, context=_ctx()))

    assert exit_code == ExitCode.PARTIAL_BROKER_FAILURE
    assert data["results"]["successful"] == 1
    assert data["results"]["failed"] == 1
    assert len(data["results"]["statuses"]) == 2


# --------------------------------------------------------------------------
# --dry-run is now a full-pipeline rehearsal (mirrors trade.py Task 3),
# threaded through propose_order/execute_order with dry_run=True.
# --------------------------------------------------------------------------
def test_batch_dry_run_is_full_pipeline_rehearsal(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    engine = _StubEngine(
        proposals=[_proposal("p1")],
        executions=[
            {
                "proposal_id": "p1",
                "ticker": "TSLA",
                "side": "buy",
                "qty": 1,
                "dry_run": True,
                "results": [
                    {"broker": "Public", "account_id": "primary", "ok": True,
                     "dry_run": True, "idempotency_key": "k1", "reason": None,
                     "detail": "dry-run ok"},
                ],
                "success_count": 1,
                "failure_count": 0,
            }
        ],
    )

    async def fake_get_engine():
        return engine

    trade_called: list = []

    async def spy_trade(*a, **kw):
        trade_called.append((a, kw))
        return True

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        batch_mod, "BROKER_FUNCTIONS", {"Public": {"trade": spy_trade}}
    )

    from_file = _write_batch_file(
        tmp_path,
        [{"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]}],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    exit_code, data = _run(
        _run_batch_from_file(args, parser=None, context=_ctx(dry_run=True))
    )

    assert len(engine.propose_calls) == 1
    assert engine.propose_calls[0]["dry_run"] is True
    assert len(engine.execute_calls) == 1
    assert engine.execute_calls[0]["dry_run"] is True
    assert trade_called == []
    assert exit_code == ExitCode.SUCCESS
    assert data["results"]["successful"] == 1


# --------------------------------------------------------------------------
# Final-review M4: text mode printed the DRY RUN banner twice (header block
# + cli_response_fn). Pin exactly one occurrence, and that JSON mode still
# carries it in `messages` (the same dedup cli/trade.py uses).
# --------------------------------------------------------------------------
def _dry_run_engine():
    return _StubEngine(
        proposals=[_proposal("p1")],
        executions=[
            {
                "proposal_id": "p1",
                "ticker": "TSLA",
                "side": "buy",
                "qty": 1,
                "dry_run": True,
                "results": [
                    {"broker": "Public", "account_id": "primary", "ok": True,
                     "dry_run": True, "idempotency_key": "k1", "reason": None,
                     "detail": "dry-run ok"},
                ],
                "success_count": 1,
                "failure_count": 0,
            }
        ],
    )


def test_batch_dry_run_banner_prints_once_in_text_mode(
    tmp_path, two_brokers, stub_session_init, monkeypatch, capsys
):
    engine = _dry_run_engine()

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [{"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]}],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    _run(
        _run_batch_from_file(
            args, parser=None, context=_ctx(dry_run=True, output_format="text")
        )
    )

    out = capsys.readouterr().out
    banner = "DRY RUN — full pipeline rehearsal, no orders placed"
    assert out.count(banner) == 1, out


def test_batch_dry_run_banner_lands_in_json_messages_once(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    engine = _dry_run_engine()

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [{"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]}],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    _, data = _run(
        _run_batch_from_file(args, parser=None, context=_ctx(dry_run=True))
    )

    banner = "DRY RUN — full pipeline rehearsal, no orders placed"
    assert data["messages"].count(banner) == 1


# --------------------------------------------------------------------------
# Mid-batch execute rejection must carry the already-completed orders'
# rendered results in `details`, not just the rejected order's fields.
# --------------------------------------------------------------------------
def test_batch_execute_rejection_carries_completed_results_in_details(
    tmp_path, two_brokers, stub_session_init, monkeypatch
):
    rejection = {
        "proposal_id": "p2",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id p2",
    }
    engine = _StubEngine(
        proposals=[_proposal("p1"), _proposal("p2")],
        executions=[
            _execution("p1", "TSLA", "buy", ok=True),
            rejection,
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(batch_mod, "get_engine", fake_get_engine)

    from_file = _write_batch_file(
        tmp_path,
        [
            {"action": "buy", "quantity": 1, "ticker": "TSLA", "brokers": ["Public"]},
            {"action": "sell", "quantity": 1, "ticker": "AAPL", "brokers": ["Public"]},
        ],
    )
    args = SimpleNamespace(action="buy", from_file=from_file, broker=None)

    with pytest.raises(batch_mod.CliRuntimeError) as excinfo:
        _run(_run_batch_from_file(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.FULL_BROKER_FAILURE
    details = excinfo.value.details
    assert details["completed_orders"] == 1
    assert details["completed_results"]["successful"] >= 1
    assert details["completed_results"]["failed"] == 0
    # Existing fields must still be present.
    assert details["reason"] == "proposal_not_found"
    assert details["proposal_id"] == "p2"
