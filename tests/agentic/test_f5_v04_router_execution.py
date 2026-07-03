"""F5 v0.4 — legacy main.py + TUI now execute through Router.execute_order
instead of order_processor.process_orders. Per-leg-token validation
(ISC-11/12/39/40) is now active on the legacy path.
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
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

ROOT = Path(__file__).resolve().parents[2]


def _fake_spec(name: str, log: list[Any]) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        log.append((name, side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    return BrokerMCPSpec(name=name, trade_fn=fake_trade, holdings_fn=fake_holdings)


@pytest.fixture
def router(tmp_path: Path) -> tuple[Router, list[Any]]:
    cli_bridge.reset_router()
    log: list[Any] = []
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    servers = {
        s.name: BrokerMCPServer(s, core=core)
        for s in (_fake_spec("FakeA", log), _fake_spec("FakeB", log))
    }
    r = Router(
        broker_servers=servers, core=core, provider=NullAccountStatusProvider()
    )
    cli_bridge._router = r  # noqa: SLF001 — test injection
    return r, log


def test_execute_via_router_returns_legacy_results_shape(
    router: tuple[Router, list[Any]],
):
    """`execute_via_router` returns the same dict shape as legacy
    `order_processor.process_orders` so callers don't need refactoring."""
    r, _ = router
    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA", "FakeB"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    results = asyncio.run(
        cli_bridge.execute_via_router(
            proposals=proposals, orders=orders, dry_run=False
        )
    )
    assert set(results.keys()) == {"successful", "failed", "skipped", "statuses"}
    assert results["successful"] == 2
    assert results["failed"] == 0
    assert results["skipped"] == 0
    assert len(results["statuses"]) == 1
    assert set(results["statuses"][0]["successful"]) == {"FakeA", "FakeB"}


def test_execute_via_router_actually_calls_broker_sdk(
    router: tuple[Router, list[Any]],
):
    """Live execution path: broker trade_fn must run exactly once per leg."""
    r, log = router
    orders = [
        {"action": "buy", "quantity": 2, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA"]},
        {"action": "buy", "quantity": 1, "ticker": "AAPL",
         "price": 10.0, "selected_brokers": ["FakeB"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    asyncio.run(
        cli_bridge.execute_via_router(
            proposals=proposals, orders=orders, dry_run=False
        )
    )
    # 1 leg each for 2 orders = 2 trade calls
    assert len(log) == 2
    by_broker = {entry[0]: entry for entry in log}
    assert by_broker["FakeA"][1] == "buy"
    assert by_broker["FakeA"][2] == 2
    assert by_broker["FakeA"][3] == "TSLA"
    assert by_broker["FakeB"][3] == "AAPL"


def test_execute_via_router_dry_run_param_mismatch_is_rejected(
    router: tuple[Router, list[Any]],
):
    """The gate proposes with dry_run=False (the only legacy main.py path —
    --dry-run is handled upstream by _build_dry_run_readiness). Trying to
    execute the same proposal as dry_run=True must fail intent_mismatch
    because the dry_run flag is part of the intent hash binding.

    This documents that the legacy bridge's dry_run parameter is effectively
    forced to False — main.py's --dry-run never reaches execute_via_router.
    """
    r, log = router
    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    results = asyncio.run(
        cli_bridge.execute_via_router(
            proposals=proposals, orders=orders, dry_run=True
        )
    )
    # Dry-run vs propose-live → intent_mismatch at the broker leg
    assert results["successful"] == 0
    assert results["failed"] == 1
    assert log == []  # broker SDK never reached


def test_execute_via_router_progress_callback_receives_per_leg_messages(
    router: tuple[Router, list[Any]],
):
    r, _ = router
    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["FakeA", "FakeB"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    messages: list[str] = []
    asyncio.run(
        cli_bridge.execute_via_router(
            proposals=proposals,
            orders=orders,
            dry_run=False,
            progress_fn=lambda msg, force_redraw=False: messages.append(msg),
        )
    )
    # Header line + per-leg ✓ lines (2 brokers)
    assert any("executing proposal" in m for m in messages)
    assert any("FakeA" in m and "✓" in m for m in messages)
    assert any("FakeB" in m and "✓" in m for m in messages)


def test_execute_via_router_partial_failure_aggregates_correctly(
    router: tuple[Router, list[Any]], tmp_path: Path
):
    """One broker SDK explodes; the aggregate must reflect partial."""
    cli_bridge.reset_router()

    async def boom(side, qty, ticker, price):
        raise RuntimeError("SDK explosion")

    async def empty(ticker=None):
        return {}

    boom_spec = BrokerMCPSpec(
        name="Boom", trade_fn=boom, holdings_fn=empty
    )
    good_log: list[Any] = []
    good_spec = _fake_spec("Good", good_log)

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "p.sqlite"),
        audit=AuditLog(tmp_path / "a.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    servers = {
        s.name: BrokerMCPServer(s, core=core) for s in (boom_spec, good_spec)
    }
    r = Router(
        broker_servers=servers, core=core, provider=NullAccountStatusProvider()
    )
    cli_bridge._router = r  # noqa: SLF001

    orders = [
        {"action": "buy", "quantity": 1, "ticker": "TSLA",
         "price": 5.0, "selected_brokers": ["Boom", "Good"]},
    ]
    proposals = asyncio.run(cli_bridge.apply_main_py_gate_batch(orders))
    results = asyncio.run(
        cli_bridge.execute_via_router(
            proposals=proposals, orders=orders, dry_run=False
        )
    )
    assert results["successful"] == 1
    assert results["failed"] == 1
    assert "Good" in results["statuses"][0]["successful"]
    assert "Boom" in results["statuses"][0]["failed"]


def test_static_main_py_no_longer_calls_order_processor_process_orders():
    """F5 v0.4 — `order_processor.process_orders(` MUST be gone from main.py.
    The import line may stay (other code may use the module); only call sites
    are forbidden. The buy/sell, batch, and automate handlers now live in the
    `cli/` package, so scan those too.
    """
    sources = [ROOT / "src" / "main.py", *sorted((ROOT / "src" / "cli").glob("*.py"))]
    for path in sources:
        src = path.read_text(encoding="utf-8")
        hits = list(re.finditer(r"order_processor\.process_orders\s*\(", src))
        assert hits == [], f"{path} still has process_orders calls: {[h.start() for h in hits]}"


def test_static_tui_no_longer_calls_order_processor_process_orders():
    """Same anti-call assertion for the TUI."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    hits = list(re.finditer(r"order_processor\.process_orders\s*\(", tui_src))
    assert hits == [], f"tui/app.py still has process_orders calls: {[h.start() for h in hits]}"


def test_static_main_py_uses_execute_via_router():
    """Positive: confirm execute_via_router is still called for the batch and
    automate paths (not yet repointed — ADR 0006 Task 4), and that the
    buy/sell path (trade.py) uses `engine.execute_order` directly (repointed
    onto the ExecutionEngine in ADR 0006 Task 3 — one propose path, one
    execute path, no bridge reshaping)."""
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    assert "engine.execute_order(" in trade_src, (
        "trade.py must call engine.execute_order directly (ADR 0006 Task 3)"
    )
    assert "execute_via_router(" not in trade_src, (
        "trade.py must not call the retired cli_bridge.execute_via_router"
    )

    other_sources = [
        ROOT / "src" / "main.py",
        *sorted(
            p for p in (ROOT / "src" / "cli").glob("*.py") if p.name != "trade.py"
        ),
    ]
    total = 0
    for path in other_sources:
        total += len(re.findall(r"execute_via_router\(", path.read_text(encoding="utf-8")))
    # from_file batch (batch.py) + automate (automate.py) — still bridge-based
    # until ADR 0006 Task 4 repoints them.
    assert total >= 2, f"main.py + cli/*.py (excl. trade.py) have only {total} execute_via_router calls"


def test_static_tui_uses_execute_via_router():
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    matches = re.findall(r"execute_via_router\(", tui_src)
    # submit_all_orders + retry_timed_out_brokers = 2 sites
    assert len(matches) >= 2, f"tui has only {len(matches)} execute_via_router calls"
