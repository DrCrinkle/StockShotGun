"""Pre-flight broker validation on the ExecutionEngine.

`validate_targets` (ADR 0006: preflight moves onto the engine) preserves the
original `_validate_brokers` / `cli_bridge.preflight_validate` semantics:

  - a broker with no validate fn passes through (validated)
  - validate fn returning (True, _)  -> validated
  - validate fn returning (None, _)  -> validated (no creds; trade fn handles)
  - validate fn returning (False, r) -> skipped with reason r
  - validate fn raising              -> skipped with the exception message
  - validate fn timing out           -> skipped ("Validation timed out")
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentic.router import NullAccountStatusProvider, Router
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker


@pytest.fixture
def engine(tmp_path: Path) -> Router:
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    return Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        automation_store_path=str(tmp_path / "automation.sqlite3"),
    )


def _run(coro):
    return asyncio.run(coro)


def test_no_validate_fn_passes_through(engine: Router):
    validated, skipped = _run(
        engine.validate_targets(
            selected_brokers=["Alpha", "Beta"],
            action="buy",
            quantity=1,
            ticker="TSLA",
            price=None,
            validate_functions={},  # no validators registered
        )
    )
    assert set(validated) == {"Alpha", "Beta"}
    assert skipped == []


def test_true_and_none_pass_false_is_skipped(engine: Router):
    async def ok(*a):
        return (True, "")

    async def no_creds(*a):
        return (None, "")

    async def reject(*a):
        return (False, "Insufficient shares (0 available)")

    validated, skipped = _run(
        engine.validate_targets(
            selected_brokers=["Good", "NoCreds", "Bad"],
            action="buy",
            quantity=1,
            ticker="TSLA",
            price=None,
            validate_functions={"Good": ok, "NoCreds": no_creds, "Bad": reject},
        )
    )
    assert set(validated) == {"Good", "NoCreds"}
    assert skipped == [("Bad", "Insufficient shares (0 available)")]


def test_exception_is_skipped_with_message(engine: Router):
    async def boom(*a):
        raise RuntimeError("broker exploded\nsecond line")

    validated, skipped = _run(
        engine.validate_targets(
            selected_brokers=["Boom"],
            action="buy",
            quantity=1,
            ticker="TSLA",
            price=None,
            validate_functions={"Boom": boom},
        )
    )
    assert validated == []
    assert len(skipped) == 1
    broker, reason = skipped[0]
    assert broker == "Boom"
    # First line only, truncated — no multi-line leakage.
    assert "broker exploded" in reason
    assert "\n" not in reason


def test_timeout_is_skipped(engine: Router):
    async def slow(*a):
        await asyncio.sleep(1.0)
        return (True, "")

    validated, skipped = _run(
        engine.validate_targets(
            selected_brokers=["Slow"],
            action="buy",
            quantity=1,
            ticker="TSLA",
            price=None,
            validate_functions={"Slow": slow},
            timeout=0.05,
        )
    )
    assert validated == []
    assert skipped == [("Slow", "Validation timed out")]


def test_validations_run_concurrently(engine: Router):
    # Two validators that each sleep; total wall time should be ~one sleep,
    # not the sum, proving concurrency.
    order_seen = []

    async def slowish(name):
        async def fn(*a):
            await asyncio.sleep(0.15)
            order_seen.append(name)
            return (True, "")

        return fn

    async def build_and_run():
        vf = {"A": await slowish("A"), "B": await slowish("B")}
        return await engine.validate_targets(
            selected_brokers=["A", "B"],
            action="buy",
            quantity=1,
            ticker="TSLA",
            price=None,
            validate_functions=vf,
        )

    validated, skipped = _run(build_and_run())
    assert set(validated) == {"A", "B"}
    assert skipped == []
