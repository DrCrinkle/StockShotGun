"""Helpers shared by more than one CLI handler module.

Anything used by 2+ of cli.sweep / cli.batch / cli.automate / cli.trade (or by
those plus the inline handlers left in main.run_cli) lives here so the handler
modules never need to import from each other in a way that creates a cycle.
"""

import asyncio
import os
import sys
from typing import Any, NoReturn

from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
)

_engine: Any = None
_engine_lock = asyncio.Lock()


async def get_engine() -> Any:
    """Lazy singleton ExecutionEngine for the CLI (ADR 0006).

    Mirrors `agentic.cli_bridge.get_router`'s lazy-singleton-with-lock
    pattern: built on demand so import-time side effects stay zero — CLI
    importers that never run a buy/sell don't pay the broker-discovery cost.
    Imported from `execution`, not `agentic`, per the ADR's canonical home.
    """
    global _engine
    async with _engine_lock:
        if _engine is None:
            from execution import ExecutionEngine

            _engine = ExecutionEngine.from_all_brokers()
        return _engine


def reset_engine() -> None:
    """Tests call this between cases to force a fresh engine. Not part of the
    documented CLI path."""
    global _engine
    _engine = None


def _raise_parser_error(parser, message, context) -> NoReturn:
    if context.output_format != "json":
        parser.print_usage(sys.stderr)
    raise CliRuntimeError(
        f"{parser.prog}: error: {message}",
        ExitCode.INVALID_ARGS,
    )


def _default_brokers_for_trade():
    brokers = []
    for broker_name in BrokerConfig.get_all_brokers():
        if broker_name in BROKER_FUNCTIONS and "trade" in BROKER_FUNCTIONS[broker_name]:
            brokers.append(broker_name)
    return brokers


def _credentials_present_for_broker(broker_name: str) -> bool:
    if broker_name == "Webull":
        webull_profiles = os.getenv("WEBULL_PROFILES")
        if webull_profiles:
            return True

    required_env_vars = BrokerConfig.get_env_vars(broker_name)
    return all(os.getenv(var) for var in required_env_vars)


def _build_dry_run_readiness(order, trade_functions):
    readiness = []
    ready_brokers = []
    for broker_name in order["selected_brokers"]:
        has_trade_function = broker_name in trade_functions
        credentials_present = _credentials_present_for_broker(broker_name)
        session_key = BrokerConfig.get_session_key(broker_name)
        session_initialized = bool(
            session_key and session_manager.sessions.get(session_key) is not None
        )
        broker_ready = has_trade_function and credentials_present
        if broker_ready:
            ready_brokers.append(broker_name)

        readiness.append(
            {
                "broker": broker_name,
                "has_trade_function": has_trade_function,
                "credentials_present": credentials_present,
                "session_initialized": session_initialized,
                "ready": broker_ready,
            }
        )

    return readiness, ready_brokers


def _mock_order_status(order):
    return {
        "successful": len(order["selected_brokers"]),
        "failed": 0,
        "skipped": 0,
        "status": {
            "successful": list(order["selected_brokers"]),
            "failed": [],
            "skipped": [],
        },
    }


def _mock_batch_results(orders):
    statuses = []
    successful = 0
    for order in orders:
        status = _mock_order_status(order)
        successful += status["successful"]
        statuses.append(status["status"])
    return {
        "successful": successful,
        "failed": 0,
        "skipped": 0,
        "statuses": statuses,
    }


def _leg_label(broker: str, account_id: str | None) -> str:
    """Render one leg's broker label.

    `"Broker"` when `account_id` is `"primary"`, empty, or `None`; otherwise
    `"Broker:account_id"`. This keeps today's single-account-per-broker output
    byte-identical to the pre-ADR-0006 `order_processor`/`execute_via_router`
    shape (which never had an account_id concept), while surfacing real
    multi-account fan-out (ADR 0001) once brokers report more than one leg.
    """
    if not account_id or account_id == "primary":
        return broker
    return f"{broker}:{account_id}"


def render_execution_result(execution: dict[str, Any]) -> dict[str, Any]:
    """Translate one `ExecutionEngine.execute_order` result into the legacy
    `order_processor`-shaped dict `{successful, failed, skipped, statuses}`
    consumed by `cli_runtime.compute_trade_exit_code` and the CLI/TUI
    printers.

    Pure function — no I/O, no engine imports. The engine's native dict is
    the contract (see docs/adr/0006-execution-engine-as-core.md Decision §3):

        {"ticker", "side", "qty", "dry_run",
         "results": [{"broker", "account_id", "ok", "dry_run",
                       "idempotency_key", "reason", "detail"}, ...],
         "success_count", "failure_count"}

    or the rejection variant (gate refused at execute time, e.g. proposal not
    found / expired / dry_run mismatch):

        {"proposal_id", "dry_run", "results": [], "success_count": 0,
         "failure_count": 0, "rejected": True, "reason", "detail"}

    Semantics pinned by ADR 0006 Task 1 (do not change without updating the
    ADR):

    1. Counts are per-LEG, not per-broker — a broker with 2 accounts (e.g.
       taxable + IRA) contributes 2 to `successful`/`failed`. This is the
       ADR's announced multi-account behavior change (ADR 0001 finally holds
       for the main CLI/TUI, not just the agent path).
    2. Each leg renders via `_leg_label`: bare broker name when
       `account_id` is `"primary"`/empty/`None`, `"Broker:account_id"`
       otherwise.
    3. `action` in the status entry comes from `execution["side"]` (`None`
       when absent, as in the rejection variant, which carries no order
       params — those live on the Proposal, not the rejection dict).
    4. Rejection variant: `results` is always `[]` on rejection (the gate
       refused before any leg was dispatched), so there are no legs to
       count — `successful=0, failed=0, skipped=0`. The rejection carries no
       broker/account list to derive a fabricated skip count from, so we
       render the honest zero rather than inventing one. `reason` (and
       `detail`) are carried into the single status entry instead, so callers
       can still surface *why* nothing happened. Note: the all-zeros
       rendering means `compute_trade_exit_code` would return SUCCESS for a
       bare rejection — callers MUST branch on `execution['rejected']` and
       raise before rendering (see run_trade for the pattern).
    5. `ok=True` legs -> `successful`; `ok=False` legs -> `failed`. The engine
       has no third "skipped" leg state at execute time — leg failures
       (breaker-open, gate rejection, broker error) all come back as
       `ok=False` with a `reason` describing which. Skips happen only at
       *propose* time (`propose_order`'s `skipped_brokers` — accounts/brokers
       that never made it into the proposal at all) and are out of scope for
       this function; `skipped` is therefore always 0 for a single rendered
       execution.
    6. `dry_run` does not alter shape or counts — a dry-run execution renders
       identically to the equivalent live one.
    """
    legs = execution.get("results") or []

    successful: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []

    for leg in legs:
        label = _leg_label(leg.get("broker", ""), leg.get("account_id"))
        if leg.get("ok"):
            successful.append(label)
        else:
            failed.append(label)

    status: dict[str, Any] = {
        "ticker": execution.get("ticker"),
        "action": execution.get("side"),
        "successful": successful,
        "failed": failed,
        "skipped": skipped,
    }
    if execution.get("rejected"):
        status["reason"] = execution.get("reason")
        status["detail"] = execution.get("detail")

    return {
        "successful": len(successful),
        "failed": len(failed),
        "skipped": len(skipped),
        "statuses": [status],
    }


def aggregate_execution_results(rendered: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum counts and concatenate `statuses` across multiple
    `render_execution_result` outputs — used by batch/automate/TUI
    multi-order paths that execute more than one order per invocation.
    """
    successful = 0
    failed = 0
    skipped = 0
    statuses: list[dict[str, Any]] = []

    for result in rendered:
        successful += int(result.get("successful", 0))
        failed += int(result.get("failed", 0))
        skipped += int(result.get("skipped", 0))
        statuses.extend(result.get("statuses", []))

    return {
        "successful": successful,
        "failed": failed,
        "skipped": skipped,
        "statuses": statuses,
    }
