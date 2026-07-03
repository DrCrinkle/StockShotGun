"""Static guardrails: `main.py`, the `cli/` handlers, and the TUI execute
orders through `ExecutionEngine.execute_order` — never through the retired
`order_processor.process_orders` fan-out or the retired
`agentic.cli_bridge.execute_via_router` bridge (F5 v0.4 / ADR 0006 step 5).

The dynamic execute-via-router tests that used to live here (legacy results
shape, per-leg broker-SDK dispatch, dry_run/intent-hash mismatch rejection,
progress-callback messages, partial-failure aggregation) exercised
`agentic.cli_bridge.execute_via_router` directly. That coverage is now
provided dynamically by `tests/test_cli_trade_golden.py`,
`tests/test_cli_batch_golden.py`, `tests/test_cli_automate_golden.py`, and
`tests/test_tui_submit_orders.py` (all stub-engine `execute_order` behavior),
plus `tests/agentic/test_preflight_validation.py` and the enforcement core's
own per-leg-token tests for the ISC-11/12/39/40 semantics. Only the
source-level regression guards survive here (ADR 0006 step 5, `cli_bridge`
deletion).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_static_main_py_no_longer_calls_order_processor_process_orders():
    """`order_processor.process_orders(` MUST be gone from main.py. The
    import line may stay (other code may use the module); only call sites
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


def test_static_main_py_uses_engine_execute_order():
    """Positive: confirm the buy/sell (trade.py), from-file batch (batch.py),
    and automate (automate.py) paths all call `engine.execute_order` directly
    — repointed onto the ExecutionEngine in ADR 0006 Task 3 (trade.py) and
    Task 4 (batch.py, automate.py). No CLI handler imports the retired
    `agentic.cli_bridge` module."""
    cli_sources = sorted((ROOT / "src" / "cli").glob("*.py"))
    import_pattern = re.compile(r"^\s*(?:from|import)\s+agentic\.cli_bridge\b", re.M)
    main_src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert not import_pattern.search(main_src), (
        "main.py must not import the retired agentic.cli_bridge module"
    )

    total = 0
    for path in cli_sources:
        src = path.read_text(encoding="utf-8")
        assert not import_pattern.search(src), (
            f"{path} must not import the retired agentic.cli_bridge module"
        )
        total += len(re.findall(r"engine\.execute_order\(", src))
    # trade.py + batch.py + automate.py — all on the engine now.
    assert total >= 3, f"cli/*.py have only {total} engine.execute_order calls"


def test_static_tui_uses_engine_execute_order():
    """Positive: confirm the TUI's `submit_orders_via_engine` helper calls
    `engine.execute_order` directly — the TUI was repointed off the retired
    `agentic.cli_bridge.execute_via_router` bridge function onto the
    ExecutionEngine in ADR 0006 Task 5. `submit_all_orders` and
    `retry_timed_out_brokers` both call the shared helper (2 call sites),
    which itself calls `engine.execute_order` once (1 definition site)."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(?:from|import)\s+agentic\.cli_bridge\b", tui_src, re.M), (
        "tui/app.py must not import the retired agentic.cli_bridge module"
    )
    helper_matches = re.findall(r"submit_orders_via_engine\(", tui_src)
    assert len(helper_matches) >= 2, (
        f"tui has only {len(helper_matches)} submit_orders_via_engine call sites"
    )
    assert "engine.execute_order(" in tui_src
