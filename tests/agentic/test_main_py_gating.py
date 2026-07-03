"""Static guardrails: the legacy `main.py` buy/sell CLI path proposes THEN
executes through the ExecutionEngine (ADR 0006) — one propose path, one
execute path, shared by every caller.

The dynamic gate-behavior tests that used to live here (minting a proposal,
rejecting per-order-limit/frozen-ticker, writing propose/execute audit
entries) exercised the retired `agentic.cli_bridge.apply_main_py_gate` /
`record_main_py_outcome` bridge functions directly. That coverage is now
provided dynamically by `tests/test_cli_trade_golden.py` (stub-engine
propose/execute assertions) and by the enforcement-core's own test suite
(limits, freeze list, audit emission) — `cli/trade.py` itself contains no
gate logic to test in isolation anymore, just calls to
`engine.propose_order` / `engine.execute_order`. Only the source-level
regression guards survive here (ADR 0006 step 5, `cli_bridge` deletion).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_static_main_py_proposes_before_executing():
    """Confirm the buy/sell handler proposes THEN executes through the
    ExecutionEngine (ADR 0006 Task 3). The buy/sell path was repointed from
    the retired `agentic.cli_bridge` onto `engine.propose_order` +
    `engine.execute_order` — one propose path, one execute path, shared by
    every caller. If a future edit removes the propose step, this test fails
    before the change can ship."""
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    assert "get_engine" in trade_src
    assert "engine.propose_order(" in trade_src
    assert "engine.execute_order(" in trade_src
    # Propose call must appear BEFORE the buy/sell `engine.execute_order(...)`
    # call.
    propose_pos = trade_src.find("engine.propose_order(")
    exec_match = re.search(r"engine\.execute_order\(", trade_src)
    assert propose_pos > 0
    assert exec_match is not None
    assert propose_pos < exec_match.start(), (
        "engine.propose_order must run BEFORE engine.execute_order"
    )


def test_static_main_py_buy_sell_path_has_no_unguarded_broker_call():
    """Scoped negative check: in the buy/sell handler body, every block that
    reaches `engine.execute_order` must be preceded by `engine.propose_order`.
    We approximate this by asserting that within the handler, the FIRST
    `engine.execute_order` call is preceded by an `engine.propose_order` call
    in the same function body.

    The buy/sell handler lives in `cli/trade.py::run_trade`; ADR 0006 Task 3
    repointed it onto the ExecutionEngine, so this guard reads that module.
    """
    trade_src = (ROOT / "src" / "cli" / "trade.py").read_text(encoding="utf-8")
    run_trade_start = trade_src.find("async def run_trade(")
    assert run_trade_start > 0
    body = trade_src[run_trade_start:]
    op = re.search(r"engine\.execute_order\(", body)
    assert op is not None, "buy/sell engine.execute_order call not found"
    upstream = body[: op.start()]
    assert "engine.propose_order(" in upstream, (
        "buy/sell path must call engine.propose_order BEFORE engine.execute_order"
    )
