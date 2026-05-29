"""Integration: pre-flight validation actually drops a failing broker BEFORE
the enforcement gate in the real (non-mock) trade path, and surfaces it as
skipped. Guards against the re-homed pre-flight being silently bypassed.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import cli.trade as trade_mod
from cli.trade import run_trade


def _run(coro):
    return asyncio.run(coro)


def test_failing_broker_dropped_before_gate(monkeypatch):
    gate_calls: list[list[str]] = []
    exec_orders: list[dict] = []

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

    async def fake_gate(*, action, quantity, ticker, price, brokers_to_use):
        gate_calls.append(list(brokers_to_use))
        return {
            "proposal_id": "p1",
            "estimated_usd": 1.0,
            "leg_count": len(brokers_to_use),
            "skipped_brokers": [],
        }

    async def fake_exec(*, proposals, orders, dry_run, progress_fn=None):
        exec_orders.extend(orders)
        return {
            "successful": 1,
            "failed": 0,
            "skipped": 0,
            "statuses": [
                {
                    "ticker": "ABC",
                    "action": "sell",
                    "successful": ["Good"],
                    "failed": [],
                    "skipped": [],
                }
            ],
        }

    async def fake_record(**kwargs):
        return None

    monkeypatch.setattr(trade_mod, "BROKER_FUNCTIONS", fake_broker_functions)
    monkeypatch.setattr(
        trade_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )
    monkeypatch.setattr(trade_mod, "apply_main_py_gate", fake_gate)
    monkeypatch.setattr(trade_mod, "execute_via_router", fake_exec)
    monkeypatch.setattr(trade_mod, "record_main_py_outcome", fake_record)

    args = SimpleNamespace(
        action="sell", quantity=5, ticker="ABC", price=1.0, broker=["Good", "Bad"]
    )
    context = SimpleNamespace(
        output_format="json", mock_brokers=False, dry_run=False
    )

    exit_code, data = _run(run_trade(args, parser=None, context=context))

    # The gate ran with ONLY the validated broker — "Bad" was dropped pre-gate.
    assert gate_calls == [["Good"]]
    # The executed order carried only the survivor.
    assert exec_orders and exec_orders[0]["selected_brokers"] == ["Good"]
    # "Bad" is reported as a validation skip, folded into results.
    assert data["validation_skipped"] == [
        {"broker": "Bad", "reason": "Insufficient shares (0 available)"}
    ]
    assert data["results"]["skipped"] == 1
    assert "Bad" in data["results"]["statuses"][0]["skipped"]


def test_all_brokers_failing_skips_gate_entirely(monkeypatch):
    gate_calls: list[list[str]] = []

    async def bad_validate(*a):
        return (False, "nope")

    async def some_trade(*a):
        return True

    async def fake_init(brokers):
        return None

    async def fake_gate(**kwargs):
        gate_calls.append(list(kwargs.get("brokers_to_use", [])))
        return {"proposal_id": "p", "estimated_usd": 0.0, "leg_count": 0, "skipped_brokers": []}

    monkeypatch.setattr(
        trade_mod, "BROKER_FUNCTIONS",
        {"Bad": {"trade": some_trade, "validate": bad_validate}},
    )
    monkeypatch.setattr(
        trade_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )
    monkeypatch.setattr(trade_mod, "apply_main_py_gate", fake_gate)

    args = SimpleNamespace(
        action="sell", quantity=5, ticker="ABC", price=1.0, broker=["Bad"]
    )
    context = SimpleNamespace(output_format="json", mock_brokers=False, dry_run=False)

    exit_code, data = _run(run_trade(args, parser=None, context=context))

    # Gate was never called — nothing survived pre-flight.
    assert gate_calls == []
    assert data["results"]["successful"] == 0
    assert data["validation_skipped"] == [{"broker": "Bad", "reason": "nope"}]
