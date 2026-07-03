"""Static guardrails: the four `main.py`/TUI order-submission call sites
(buy/sell, from-file batch, automate, TUI submit/retry) propose every order
through the ExecutionEngine BEFORE executing any of them (ADR 0006 Task 4/5).

The dynamic gate-batch tests that used to live here (minting one proposal per
order, aborting the whole batch on the first rejection, writing one audit
entry per order) exercised the retired `agentic.cli_bridge.
apply_main_py_gate_batch` / `record_main_py_outcome_batch` bridge functions
directly. That coverage is now provided dynamically by
`tests/test_cli_batch_golden.py` and `tests/test_cli_automate_golden.py`
(stub-engine propose/execute-per-order assertions, fail-fast-on-first-
rejection) and `tests/test_tui_submit_orders.py` (two-phase propose-then-
execute ordering for the TUI). Only the source-level regression guards
survive here (ADR 0006 step 5, `cli_bridge` deletion).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_static_main_py_batch_path_is_gated():
    """The from-file batch path's `engine.execute_order` calls MUST be
    preceded by `engine.propose_order` calls in the same function body (ADR
    0006 Task 4 repointed batch.py from the retired `agentic.cli_bridge`
    bridge functions onto the ExecutionEngine directly — one propose path,
    one execute path, per order)."""
    batch_src = (ROOT / "src" / "cli" / "batch.py").read_text(encoding="utf-8")
    fn_start = batch_src.find("async def _run_batch_from_file(")
    assert fn_start > 0
    fn_end = batch_src.find("\nasync def ", fn_start + 1)
    body = batch_src[fn_start:fn_end if fn_end > 0 else None]
    assert "engine.propose_order(" in body
    assert "engine.execute_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert gate_pos < op_pos


def test_static_main_py_automate_path_is_gated():
    """The automate path's `engine.execute_order` calls MUST be preceded by
    `engine.propose_order` calls (ADR 0006 Task 4 repointed automate.py onto
    the ExecutionEngine directly)."""
    automate_src = (ROOT / "src" / "cli" / "automate.py").read_text(encoding="utf-8")
    fn_start = automate_src.find("async def _run_automate_from_recap(")
    assert fn_start > 0
    fn_end = automate_src.find("\nasync def ", fn_start + 1)
    body = automate_src[fn_start:fn_end if fn_end > 0 else None]
    assert "engine.propose_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert op_pos > 0
    assert gate_pos < op_pos


def test_static_tui_submit_orders_path_is_gated():
    """TUI's submit_all_orders MUST propose (gate) via the ExecutionEngine
    before executing (ADR 0006 Task 5 repointed the TUI off the retired
    `agentic.cli_bridge` batch-gate/router-execute functions onto
    `submit_orders_via_engine`, which calls `engine.propose_order` for
    every order before calling `engine.execute_order` for any of them)."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def submit_all_orders(")
    assert fn_start > 0
    # The next `async def` or `def` at the same indent terminates the function.
    fn_end = re.search(r"\n    (?:async )?def ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "submit_orders_via_engine(" in body


def test_static_tui_retry_path_is_gated():
    """TUI's retry_timed_out_brokers MUST propose (gate) via the
    ExecutionEngine before executing — same helper as submit_all_orders."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def retry_timed_out_brokers(")
    assert fn_start > 0
    fn_end = re.search(r"\n    (?:async )?def ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "submit_orders_via_engine(" in body


def test_static_submit_orders_via_engine_proposes_before_executing():
    """The shared helper itself must propose every order before executing
    any of them — this is where the fail-fast-before-fan-out contract lives
    (ADR 0006 Task 5 centralizes it in one helper, replacing the retired
    `apply_main_py_gate_batch` -> `execute_via_router` bridge pairing)."""
    tui_src = (ROOT / "src" / "tui" / "app.py").read_text(encoding="utf-8")
    fn_start = tui_src.find("async def submit_orders_via_engine(")
    assert fn_start > 0
    fn_end = re.search(r"\nasync def |\ndef ", tui_src[fn_start + 1 :])
    body = tui_src[fn_start : fn_start + 1 + (fn_end.start() if fn_end else 0)]
    assert "engine.propose_order(" in body
    assert "engine.execute_order(" in body
    gate_pos = body.find("engine.propose_order(")
    op_pos = body.find("engine.execute_order(")
    assert gate_pos < op_pos
