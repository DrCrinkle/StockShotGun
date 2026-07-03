"""ADR-0006 step-0 characterization ("golden") tests for the main CLI trade path.

These LOCK the *current* observable behavior of `cli.trade.run_trade` so that
when ADR 0006 (Execution Engine as Core) repoints the CLI through the engine,
the behavior changes show up as intentional, reviewable diffs rather than silent
regressions.

What is pinned here:
  * the `--mock-brokers` result envelope (shape + exit code) — unchanged by
    ADR 0006 Task 3.
  * the `--dry-run` branch is now a FULL-PIPELINE REHEARSAL (ADR 0006 Task 3):
    it flows through the same `engine.propose_order` / `engine.execute_order`
    path as a live order, with `dry_run=True` bound end to end. The old
    credentials-only readiness short-circuit is retired.
  * multi-account fan-out: a mocked engine returning legs across more than one
    account per broker renders per-LEG counts/labels (ADR 0001 finally holding
    for the main CLI).

Harness mirrors tests/agentic/test_preflight_integration.py: import run_trade,
drive it in-process with SimpleNamespace args/context, assert on (exit_code, data).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import cli.trade as trade_mod
from cli.trade import run_trade
from cli_runtime import ExitCode


def _run(coro):
    return asyncio.run(coro)


async def _some_trade(*a):
    return True


@pytest.fixture
def two_brokers(monkeypatch):
    """Register two trade-capable brokers in the handler's registry view."""
    monkeypatch.setattr(
        trade_mod,
        "BROKER_FUNCTIONS",
        {
            "Public": {"trade": _some_trade},
            "Robinhood": {"trade": _some_trade},
        },
    )


def _ctx(**over):
    base = dict(output_format="json", mock_brokers=False, dry_run=False)
    base.update(over)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------
# Mock path — locks the _mock_batch_results envelope exactly.
# --------------------------------------------------------------------------
def test_mock_buy_envelope_is_pinned(two_brokers):
    args = SimpleNamespace(
        action="buy", quantity=10, ticker="TSLA", price=None,
        broker=["Public", "Robinhood"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(mock_brokers=True)))

    assert exit_code == ExitCode.SUCCESS
    assert data == {
        "mock": True,
        "order": {
            "action": "buy",
            "quantity": 10,
            "ticker": "TSLA",
            "price": None,
            "selected_brokers": ["Public", "Robinhood"],
        },
        "results": {
            "successful": 2,
            "failed": 0,
            "skipped": 0,
            "statuses": [
                {"successful": ["Public", "Robinhood"], "failed": [], "skipped": []}
            ],
        },
        "messages": ["Mock mode: no live broker calls were executed"],
    }


def test_mock_sell_with_limit_price_is_pinned(two_brokers):
    args = SimpleNamespace(
        action="sell", quantity=5, ticker="AAPL", price=175.5,
        broker=["Public"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(mock_brokers=True)))

    assert exit_code == ExitCode.SUCCESS
    assert data["order"] == {
        "action": "sell",
        "quantity": 5,
        "ticker": "AAPL",
        "price": 175.5,
        "selected_brokers": ["Public"],
    }
    # One leg per broker name — the current "primary"-only fan-out (ADR 0006 #1).
    assert data["results"]["statuses"] == [
        {"successful": ["Public"], "failed": [], "skipped": []}
    ]


# --------------------------------------------------------------------------
# Dry-run path — locks "full pipeline rehearsal" (ADR 0006 Task 3).
# --------------------------------------------------------------------------
class _StubEngine:
    """Records propose/execute calls; returns a canned single-leg execution."""

    def __init__(self, proposal=None, execution=None):
        self.propose_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self._proposal = proposal or {
            "proposal_id": "prop-1",
            "estimated_usd": 1.0,
            "leg_count": 2,
            "accounts_by_broker": {"Public": ["primary"], "Robinhood": ["primary"]},
            "skipped_brokers": [],
        }
        self._execution = execution or {
            "ticker": "XYZ",
            "side": "buy",
            "qty": 1,
            "dry_run": True,
            "results": [
                {"broker": "Public", "account_id": "primary", "ok": True,
                 "dry_run": True, "idempotency_key": "k1", "reason": None, "detail": "dry-run ok"},
                {"broker": "Robinhood", "account_id": "primary", "ok": True,
                 "dry_run": True, "idempotency_key": "k2", "reason": None, "detail": "dry-run ok"},
            ],
            "success_count": 2,
            "failure_count": 0,
        }

    async def validate_targets(self, **kwargs):
        return list(kwargs["selected_brokers"]), []

    async def propose_order(self, **kwargs):
        self.propose_calls.append(kwargs)
        return self._proposal

    async def execute_order(self, **kwargs):
        self.execute_calls.append(kwargs)
        return self._execution


@pytest.fixture
def stub_session_init(monkeypatch):
    async def fake_init(brokers):
        return None

    monkeypatch.setattr(
        trade_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )


def test_dry_run_is_full_pipeline_rehearsal(two_brokers, stub_session_init, monkeypatch):
    engine = _StubEngine()

    async def fake_get_engine():
        return engine

    trade_called: list = []

    async def spy_trade(*a, **kw):
        trade_called.append((a, kw))
        return True

    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {
            "Public": {"trade": spy_trade},
            "Robinhood": {"trade": spy_trade},
        },
    )

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="XYZ", price=None,
        broker=["Public", "Robinhood"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(dry_run=True)))

    # Full pipeline: propose AND execute both ran, both bound to dry_run=True.
    assert len(engine.propose_calls) == 1
    assert engine.propose_calls[0]["dry_run"] is True
    assert len(engine.execute_calls) == 1
    assert engine.execute_calls[0]["dry_run"] is True
    assert engine.execute_calls[0]["proposal_id"] == "prop-1"

    # No broker trade function was ever invoked directly — the engine's
    # in-process broker port simulated the dry-run leg, not the CLI.
    assert trade_called == []

    assert exit_code == ExitCode.SUCCESS
    assert data["dry_run"] is True
    assert data["results"]["successful"] == 2
    assert data["results"]["failed"] == 0
    # Rehearsal labeling is visible in the rendered envelope.
    assert any(
        "DRY RUN" in m and "rehearsal" in m.lower() for m in data.get("messages", [])
    )


def test_multi_account_fan_out_renders_per_leg(two_brokers, stub_session_init, monkeypatch):
    proposal = {
        "proposal_id": "prop-multi",
        "estimated_usd": 3.0,
        "leg_count": 3,
        "accounts_by_broker": {"Robinhood": ["taxable", "ira"], "Public": ["primary"]},
        "skipped_brokers": [],
    }
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [
            {"broker": "Robinhood", "account_id": "taxable", "ok": True,
             "dry_run": False, "idempotency_key": "k1", "reason": None, "detail": "placed"},
            {"broker": "Robinhood", "account_id": "ira", "ok": True,
             "dry_run": False, "idempotency_key": "k2", "reason": None, "detail": "placed"},
            {"broker": "Public", "account_id": "primary", "ok": True,
             "dry_run": False, "idempotency_key": "k3", "reason": None, "detail": "placed"},
        ],
        "success_count": 3,
        "failure_count": 0,
    }
    engine = _StubEngine(proposal=proposal, execution=execution)

    async def fake_get_engine():
        return engine

    async def some_trade(*a):
        return True

    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {
            "Public": {"trade": some_trade},
            "Robinhood": {"trade": some_trade},
        },
    )

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="TSLA", price=None,
        broker=["Public", "Robinhood"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(dry_run=False)))

    assert exit_code == ExitCode.SUCCESS
    assert data["results"]["successful"] == 3
    assert data["results"]["failed"] == 0
    assert data["results"]["statuses"][0]["successful"] == [
        "Robinhood:taxable", "Robinhood:ira", "Public",
    ]


# --------------------------------------------------------------------------
# Execute-time rejection — must NOT read as success (proposal expired,
# dry_run mismatch, proposal not found).
# --------------------------------------------------------------------------
def test_execute_rejection_is_not_success(two_brokers, stub_session_init, monkeypatch):
    execution = {
        "proposal_id": "prop-1",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id prop-1",
    }
    engine = _StubEngine(execution=execution)

    async def fake_get_engine():
        return engine

    async def some_trade(*a):
        return True

    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {
            "Public": {"trade": some_trade},
            "Robinhood": {"trade": some_trade},
        },
    )

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="XYZ", price=None,
        broker=["Public", "Robinhood"],
    )

    with pytest.raises(trade_mod.CliRuntimeError) as excinfo:
        _run(run_trade(args, parser=None, context=_ctx(dry_run=False)))

    assert excinfo.value.exit_code == ExitCode.FULL_BROKER_FAILURE
    assert excinfo.value.exit_code != ExitCode.SUCCESS


# --------------------------------------------------------------------------
# Dry-run + no ready brokers — dry-run and live must agree on exit code
# (rehearsal-world equivalent of the deleted
# test_dry_run_no_ready_brokers_maps_to_credential_exit, which pinned the
# old credentials-only readiness short-circuit. That path is retired by ADR
# 0006 Task 3; pre-flight validation is now the single gate for both dry-run
# and live. Mirrors
# tests/agentic/test_preflight_integration.py::test_all_brokers_failing_skips_gate_entirely.)
# --------------------------------------------------------------------------
def test_dry_run_no_ready_brokers_matches_live_exit_code(stub_session_init, monkeypatch):
    async def bad_validate(*a):
        return (False, "nope")

    def _make_engine():
        engine = _StubEngine()

        async def validate_targets_all_fail(**kwargs):
            return [], [(b, "nope") for b in kwargs["selected_brokers"]]

        engine.validate_targets = validate_targets_all_fail
        return engine

    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {"Public": {"trade": _some_trade, "validate": bad_validate}},
    )

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="XYZ", price=None, broker=["Public"],
    )

    # Live path: no brokers survive pre-flight -> propose_order never called.
    live_engine = _make_engine()

    async def fake_get_engine_live():
        return live_engine

    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine_live)
    exit_code_live, data_live = _run(
        run_trade(args, parser=None, context=_ctx(dry_run=False))
    )
    assert live_engine.propose_calls == []
    assert data_live["results"]["successful"] == 0

    # Dry-run path: same pre-flight gate, same outcome — no brokers survive,
    # so propose/execute never run even under rehearsal.
    dry_engine = _make_engine()

    async def fake_get_engine_dry():
        return dry_engine

    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine_dry)
    exit_code_dry, data_dry = _run(
        run_trade(args, parser=None, context=_ctx(dry_run=True))
    )
    assert dry_engine.propose_calls == []
    assert data_dry["results"]["successful"] == 0

    # Dry-run and live now agree on the exit code (both hit the same
    # pre-flight-exhausted branch) — this is the behavior ADR 0006 Task 3
    # unified; assert the SAME constant, not just "both non-success".
    assert exit_code_dry == exit_code_live
    assert exit_code_live == ExitCode.CONFIG_CREDENTIAL_MISSING
