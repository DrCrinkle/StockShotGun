"""ADR-0006 step-0 characterization ("golden") tests for the main CLI trade path.

These LOCK the *current* observable behavior of `cli.trade.run_trade` so that
when ADR 0006 (Execution Engine as Core) repoints the CLI through the engine,
the behavior changes show up as intentional, reviewable diffs rather than silent
regressions.

What is pinned here:
  * the `--mock-brokers` result envelope (shape + exit code)
  * the `--dry-run` branch returns a *readiness* report and NEVER invokes the
    enforcement gate or the router (ADR 0006 Context #2). When the redesign makes
    `--dry-run` a full-pipeline rehearsal, `test_dry_run_does_not_touch_gate`
    is expected to change — that's the signal.

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
# Dry-run path — locks "readiness only, gate/router untouched" (ADR 0006 #2).
# --------------------------------------------------------------------------
def test_dry_run_does_not_touch_gate(two_brokers, monkeypatch):
    gate_calls: list = []
    exec_calls: list = []

    async def spy_gate(**kwargs):
        gate_calls.append(kwargs)
        raise AssertionError("gate must not run on the --dry-run path")

    async def spy_exec(**kwargs):
        exec_calls.append(kwargs)
        raise AssertionError("router execute must not run on the --dry-run path")

    # Deterministic readiness so the envelope doesn't depend on ambient env creds.
    def fake_readiness(order, trade_functions):
        readiness = [
            {
                "broker": b,
                "has_trade_function": True,
                "credentials_present": True,
                "session_initialized": False,
                "ready": True,
            }
            for b in order["selected_brokers"]
        ]
        return readiness, list(order["selected_brokers"])

    monkeypatch.setattr(trade_mod, "apply_main_py_gate", spy_gate)
    monkeypatch.setattr(trade_mod, "execute_via_router", spy_exec)
    monkeypatch.setattr(trade_mod, "_build_dry_run_readiness", fake_readiness)

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="XYZ", price=None,
        broker=["Public", "Robinhood"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(dry_run=True)))

    # The defining fact ADR 0006 will change: dry-run is a readiness probe only.
    assert gate_calls == []
    assert exec_calls == []
    assert exit_code == ExitCode.SUCCESS
    assert data["dry_run"] is True
    assert data["ready_brokers"] == ["Public", "Robinhood"]
    assert [r["broker"] for r in data["readiness"]] == ["Public", "Robinhood"]
    # No execution-results key on the dry-run envelope.
    assert "results" not in data


def test_dry_run_no_ready_brokers_maps_to_credential_exit(two_brokers, monkeypatch):
    def fake_readiness(order, trade_functions):
        readiness = [
            {
                "broker": b,
                "has_trade_function": True,
                "credentials_present": False,
                "session_initialized": False,
                "ready": False,
            }
            for b in order["selected_brokers"]
        ]
        return readiness, []  # nothing ready

    monkeypatch.setattr(trade_mod, "_build_dry_run_readiness", fake_readiness)

    args = SimpleNamespace(
        action="buy", quantity=1, ticker="XYZ", price=None, broker=["Public"],
    )
    exit_code, data = _run(run_trade(args, parser=None, context=_ctx(dry_run=True)))

    assert exit_code == ExitCode.CONFIG_CREDENTIAL_MISSING
    assert data["dry_run"] is True
    assert data["ready_brokers"] == []
