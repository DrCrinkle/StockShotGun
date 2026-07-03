"""Integration: pre-flight validation actually drops a failing broker BEFORE
the engine propose/execute path, and surfaces it as skipped. Guards against
the re-homed pre-flight (now `engine.validate_targets`, ADR 0006) being
silently bypassed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cli.trade as trade_mod
from cli.trade import run_trade


def _run(coro):
    return asyncio.run(coro)


class _FakeEngine:
    """Stub ExecutionEngine recording validate/propose/execute calls."""

    def __init__(self, *, validated, skipped, execution):
        self._validated = validated
        self._skipped = skipped
        self._execution = execution
        self.validate_calls: list[dict] = []
        self.propose_calls: list[dict] = []
        self.execute_calls: list[dict] = []

    async def validate_targets(self, **kwargs):
        self.validate_calls.append(kwargs)
        return self._validated, self._skipped

    async def propose_order(self, **kwargs):
        self.propose_calls.append(kwargs)
        return {
            "proposal_id": "p1",
            "estimated_usd": 1.0,
            "leg_count": len(kwargs.get("brokers") or []),
            "accounts_by_broker": {
                b: ["primary"] for b in (kwargs.get("brokers") or [])
            },
            "skipped_brokers": [],
        }

    async def execute_order(self, **kwargs):
        self.execute_calls.append(kwargs)
        return self._execution


def test_failing_broker_dropped_before_gate(monkeypatch):
    async def ok_validate(*a):
        return (True, "")

    async def bad_validate(*a):
        return (False, "Insufficient shares (0 available)")

    async def some_trade(*a):
        return True

    fake_broker_functions = {
        "Good": {"trade": some_trade, "validate": ok_validate},
        "Bad": {"trade": some_trade, "validate": bad_validate},
    }

    async def fake_init(brokers):
        return None

    engine = _FakeEngine(
        validated=["Good"],
        skipped=[("Bad", "Insufficient shares (0 available)")],
        execution={
            "ticker": "ABC",
            "side": "sell",
            "qty": 5,
            "dry_run": False,
            "results": [
                {"broker": "Good", "account_id": "primary", "ok": True,
                 "dry_run": False, "idempotency_key": "k1", "reason": None,
                 "detail": "placed"},
            ],
            "success_count": 1,
            "failure_count": 0,
        },
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(trade_mod, "BROKER_FUNCTIONS", fake_broker_functions)
    monkeypatch.setattr(
        trade_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )
    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine)

    args = SimpleNamespace(
        action="sell", quantity=5, ticker="ABC", price=1.0, broker=["Good", "Bad"]
    )
    context = SimpleNamespace(
        output_format="json", mock_brokers=False, dry_run=False
    )

    exit_code, data = _run(run_trade(args, parser=None, context=context))

    # The engine was proposed to with ONLY the validated broker — "Bad" was
    # dropped pre-propose.
    assert engine.propose_calls and engine.propose_calls[0]["brokers"] == ["Good"]
    # "Bad" is reported as a validation skip, folded into results.
    assert data["validation_skipped"] == [
        {"broker": "Bad", "reason": "Insufficient shares (0 available)"}
    ]
    assert data["results"]["skipped"] == 1
    assert "Bad" in data["results"]["statuses"][0]["skipped"]


def test_all_brokers_failing_skips_gate_entirely(monkeypatch):
    async def bad_validate(*a):
        return (False, "nope")

    async def some_trade(*a):
        return True

    async def fake_init(brokers):
        return None

    engine = _FakeEngine(validated=[], skipped=[("Bad", "nope")], execution={})

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {"Bad": {"trade": some_trade, "validate": bad_validate}},
    )
    monkeypatch.setattr(
        trade_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )
    monkeypatch.setattr(trade_mod, "get_engine", fake_get_engine)

    args = SimpleNamespace(
        action="sell", quantity=5, ticker="ABC", price=1.0, broker=["Bad"]
    )
    context = SimpleNamespace(output_format="json", mock_brokers=False, dry_run=False)

    exit_code, data = _run(run_trade(args, parser=None, context=context))

    # propose_order was never called — nothing survived pre-flight.
    assert engine.propose_calls == []
    assert data["results"]["successful"] == 0
    assert data["validation_skipped"] == [{"broker": "Bad", "reason": "nope"}]
