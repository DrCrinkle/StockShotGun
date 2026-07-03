"""Tests for the ADR-0006 Task 1 rendering layer.

`render_execution_result` / `aggregate_execution_results` (src/cli/common.py)
translate the ExecutionEngine's native per-leg result shape into the legacy
`order_processor`-shaped dict (`{successful, failed, skipped, statuses}`) that
`cli_runtime.compute_trade_exit_code` and the CLI/TUI printers consume.

Pinned semantics (see docstring on `render_execution_result` for the
authoritative statement):
  1. Counts are per-LEG, not per-broker (ADR 0006's announced multi-account
     behavior change).
  2. Leg label is `"Broker"` when account_id is `"primary"`/empty/None,
     `"Broker:account_id"` otherwise — single-account output stays
     byte-identical to the pre-ADR-0006 `order_processor` shape.
  3. `action` comes from the execution's `side`.
  4. The rejection variant (`rejected: True`, `results: []`) has no legs to
     count, so it renders `successful=0, failed=0, skipped=0` and carries
     `reason`/`detail` into the status entry instead of fabricating a skip
     count the engine never gave us.
  5. `aggregate_execution_results` sums counts and concatenates `statuses`.
  6. `dry_run` does not change shape or counts.
"""

from __future__ import annotations

from cli.common import aggregate_execution_results, render_execution_result
from cli_runtime import ExitCode, compute_trade_exit_code


def _leg(broker, account_id, ok, reason=None, detail="placed", dry_run=False):
    return {
        "broker": broker,
        "account_id": account_id,
        "ok": ok,
        "dry_run": dry_run,
        "idempotency_key": "idem-1",
        "reason": reason,
        "detail": detail,
    }


# --------------------------------------------------------------------------
# Multi-account per-leg counting
# --------------------------------------------------------------------------


def test_multi_account_counts_are_per_leg_not_per_broker():
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 10,
        "dry_run": False,
        "results": [
            _leg("Robinhood", "taxable", ok=True),
            _leg("Robinhood", "ira", ok=False, reason="freeze_list"),
            _leg("Public", "primary", ok=True),
        ],
        "success_count": 2,
        "failure_count": 1,
    }

    rendered = render_execution_result(execution)

    # 3 legs total: 2 ok, 1 failed — NOT 2 brokers.
    assert rendered["successful"] == 2
    assert rendered["failed"] == 1
    assert rendered["skipped"] == 0

    assert len(rendered["statuses"]) == 1
    status = rendered["statuses"][0]
    assert status["ticker"] == "TSLA"
    assert status["action"] == "buy"
    assert status["successful"] == ["Robinhood:taxable", "Public"]
    assert status["failed"] == ["Robinhood:ira"]
    assert status["skipped"] == []


# --------------------------------------------------------------------------
# Label collapsing: primary / empty / None account_id -> bare broker name
# --------------------------------------------------------------------------


def test_primary_and_empty_account_labels_collapse_to_bare_broker_name():
    execution = {
        "ticker": "AAPL",
        "side": "sell",
        "qty": 5,
        "dry_run": False,
        "results": [
            _leg("Public", "primary", ok=True),
            _leg("Fennel", "", ok=True),
            _leg("Tradier", None, ok=True),
            _leg("Robinhood", "ira", ok=True),
        ],
        "success_count": 4,
        "failure_count": 0,
    }

    rendered = render_execution_result(execution)

    status = rendered["statuses"][0]
    assert status["successful"] == ["Public", "Fennel", "Tradier", "Robinhood:ira"]
    assert rendered["successful"] == 4
    assert rendered["failed"] == 0


def test_single_account_output_is_byte_identical_to_legacy_shape():
    """Today's (pre-ADR-0006) single-account-per-broker case must render
    exactly like the old `execute_via_router` output: bare broker names, no
    `:primary` suffix anywhere.
    """
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [
            _leg("Public", "primary", ok=True),
            _leg("Robinhood", "primary", ok=True),
        ],
        "success_count": 2,
        "failure_count": 0,
    }

    rendered = render_execution_result(execution)

    assert rendered == {
        "successful": 2,
        "failed": 0,
        "skipped": 0,
        "statuses": [
            {
                "ticker": "TSLA",
                "action": "buy",
                "successful": ["Public", "Robinhood"],
                "failed": [],
                "skipped": [],
            }
        ],
    }


# --------------------------------------------------------------------------
# Rejection variant
# --------------------------------------------------------------------------


def test_rejection_variant_renders_zero_counts_and_carries_reason():
    execution = {
        "proposal_id": "prop-123",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id prop-123",
    }

    rendered = render_execution_result(execution)

    assert rendered["successful"] == 0
    assert rendered["failed"] == 0
    assert rendered["skipped"] == 0
    assert len(rendered["statuses"]) == 1
    status = rendered["statuses"][0]
    assert status["successful"] == []
    assert status["failed"] == []
    assert status["skipped"] == []
    assert status["reason"] == "proposal_not_found"


def test_rejection_variant_action_falls_back_when_side_absent():
    # The rejection dict from execute_order carries no `ticker`/`side` (those
    # only exist on the proposal, which the caller may not have merged in).
    execution = {
        "proposal_id": "prop-999",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "empty_proposal",
        "detail": "proposal has no legs",
    }

    rendered = render_execution_result(execution)
    status = rendered["statuses"][0]
    assert status["ticker"] is None
    assert status["action"] is None
    assert status["reason"] == "empty_proposal"


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def test_aggregate_execution_results_sums_counts_and_concatenates_statuses():
    r1 = render_execution_result(
        {
            "ticker": "TSLA",
            "side": "buy",
            "qty": 1,
            "dry_run": False,
            "results": [_leg("Public", "primary", ok=True)],
            "success_count": 1,
            "failure_count": 0,
        }
    )
    r2 = render_execution_result(
        {
            "ticker": "AAPL",
            "side": "sell",
            "qty": 2,
            "dry_run": False,
            "results": [
                _leg("Robinhood", "taxable", ok=False, reason="freeze_list"),
                _leg("Robinhood", "ira", ok=True),
            ],
            "success_count": 1,
            "failure_count": 1,
        }
    )
    r3 = render_execution_result(
        {
            "proposal_id": "prop-1",
            "dry_run": False,
            "results": [],
            "success_count": 0,
            "failure_count": 0,
            "rejected": True,
            "reason": "dry_run_mismatch",
            "detail": "mismatch",
        }
    )

    aggregated = aggregate_execution_results([r1, r2, r3])

    assert aggregated["successful"] == 2
    assert aggregated["failed"] == 1
    assert aggregated["skipped"] == 0
    assert aggregated["statuses"] == r1["statuses"] + r2["statuses"] + r3["statuses"]
    assert len(aggregated["statuses"]) == 3


def test_aggregate_of_empty_list_is_zeroed():
    aggregated = aggregate_execution_results([])
    assert aggregated == {"successful": 0, "failed": 0, "skipped": 0, "statuses": []}


# --------------------------------------------------------------------------
# dry_run invariance
# --------------------------------------------------------------------------


def test_dry_run_execution_renders_identically_to_live():
    live = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [_leg("Public", "primary", ok=True, dry_run=False)],
        "success_count": 1,
        "failure_count": 0,
    }
    dry = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": True,
        "results": [_leg("Public", "primary", ok=True, dry_run=True)],
        "success_count": 1,
        "failure_count": 0,
    }

    rendered_live = render_execution_result(live)
    rendered_dry = render_execution_result(dry)

    assert rendered_live == rendered_dry


# --------------------------------------------------------------------------
# Direct integration with compute_trade_exit_code
# --------------------------------------------------------------------------


def test_all_ok_execution_yields_success_exit_code():
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [
            _leg("Public", "primary", ok=True),
            _leg("Robinhood", "primary", ok=True),
        ],
        "success_count": 2,
        "failure_count": 0,
    }

    rendered = render_execution_result(execution)

    assert compute_trade_exit_code(rendered) == ExitCode.SUCCESS


def test_mixed_execution_yields_partial_broker_failure_exit_code():
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [
            _leg("Public", "primary", ok=True),
            _leg("Robinhood", "primary", ok=False, reason="rejected"),
        ],
        "success_count": 1,
        "failure_count": 1,
    }

    rendered = render_execution_result(execution)

    assert compute_trade_exit_code(rendered) == ExitCode.PARTIAL_BROKER_FAILURE


def test_all_failed_execution_yields_full_broker_failure_exit_code():
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [
            _leg("Public", "primary", ok=False, reason="rejected"),
            _leg("Robinhood", "primary", ok=False, reason="rejected"),
        ],
        "success_count": 0,
        "failure_count": 2,
    }

    rendered = render_execution_result(execution)

    assert compute_trade_exit_code(rendered) == ExitCode.FULL_BROKER_FAILURE


def test_rejection_render_alone_maps_to_success_exit_code_hence_callers_must_branch():
    """Pins the footgun documented in `render_execution_result`'s docstring
    §4: an all-zeros rendering (successful=0, failed=0, skipped=0) is
    indistinguishable from "nothing to do, all good" as far as
    `compute_trade_exit_code` is concerned — it returns SUCCESS. Callers
    (see `run_trade`) MUST check `execution["rejected"]` and raise BEFORE
    calling `render_execution_result`, never rely on the rendered/exit-code
    layer to surface a rejection. If this test starts failing, either the
    exit-code computation changed (update the docstring contract) or someone
    "fixed" this by inventing a fabricated non-zero count (don't).
    """
    execution = {
        "proposal_id": "prop-123",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id prop-123",
    }

    rendered = render_execution_result(execution)

    assert compute_trade_exit_code(rendered) == ExitCode.SUCCESS


# --------------------------------------------------------------------------
# Edge cases: empty results without rejection, unknown extra keys ignored
# --------------------------------------------------------------------------


def test_empty_results_without_rejected_flag_renders_zeros():
    execution = {
        "ticker": "X",
        "side": "buy",
        "results": [],
        "success_count": 0,
        "failure_count": 0,
    }

    rendered = render_execution_result(execution)

    assert rendered["successful"] == 0
    assert rendered["failed"] == 0
    assert rendered["skipped"] == 0
    status = rendered["statuses"][0]
    assert status["successful"] == []
    assert status["failed"] == []
    assert status["skipped"] == []
    assert "reason" not in status


def test_unknown_extra_keys_are_ignored():
    execution = {
        "ticker": "TSLA",
        "side": "buy",
        "qty": 1,
        "dry_run": False,
        "results": [_leg("Public", "primary", ok=True)],
        "success_count": 1,
        "failure_count": 0,
    }
    execution_with_extra = dict(execution, warnings=["some warning"])

    assert render_execution_result(execution_with_extra) == render_execution_result(
        execution
    )
