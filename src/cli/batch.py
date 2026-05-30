"""`--from-file` batch order handler: validate a JSON order file, then gate and
execute every order through the Router."""

import json
from typing import Any, cast

from agentic.cli_bridge import (
    apply_main_py_gate_batch,
    execute_via_router,
    gate_error_to_exit_code,
    preflight_validate,
    record_main_py_outcome_batch,
)
from enforcement import GateError
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
    compute_trade_exit_code,
)
from brokers import session_manager  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()

from cli.common import (
    _build_dry_run_readiness,
    _default_brokers_for_trade,
    _mock_batch_results,
    _raise_parser_error,
)


def _validate_batch_orders(file_path, parser):
    try:
        with open(file_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError as exc:
        raise CliRuntimeError(
            f"Batch file not found: {file_path}",
            ExitCode.INVALID_ARGS,
            details={"file": file_path},
        ) from exc
    except json.JSONDecodeError as exc:
        raise CliRuntimeError(
            f"Invalid JSON in batch file: {file_path}",
            ExitCode.INVALID_ARGS,
            details={"file": file_path, "error": str(exc)},
        ) from exc

    if isinstance(payload, dict):
        payload = payload.get("orders")

    if not isinstance(payload, list):
        raise CliRuntimeError(
            'Batch file must contain an order list or {"orders": [...]} object',
            ExitCode.INVALID_ARGS,
            details={"file": file_path},
        )

    default_brokers = _default_brokers_for_trade()
    if not default_brokers:
        raise CliRuntimeError(
            "No broker credentials configured",
            ExitCode.CONFIG_CREDENTIAL_MISSING,
        )

    validation_errors = []
    normalized_orders = []
    selected_union = set()

    for index, raw_order in enumerate(payload, start=1):
        prefix = f"order[{index}]"
        if not isinstance(raw_order, dict):
            validation_errors.append(f"{prefix}: must be an object")
            continue

        order = cast("dict[str, Any]", raw_order)
        action = order.get("action")
        quantity = order.get("quantity")
        ticker = order.get("ticker")
        price = order.get("price")
        brokers = order.get("brokers")

        if action not in {"buy", "sell"}:
            validation_errors.append(f"{prefix}: action must be 'buy' or 'sell'")
            continue

        if not isinstance(quantity, int) or quantity <= 0:
            validation_errors.append(f"{prefix}: quantity must be a positive integer")
            continue

        if not isinstance(ticker, str) or not ticker.strip():
            validation_errors.append(f"{prefix}: ticker must be a non-empty string")
            continue

        if price is not None and not isinstance(price, (int, float)):
            validation_errors.append(f"{prefix}: price must be numeric when provided")
            continue

        selected_brokers = default_brokers
        if brokers is not None:
            if not isinstance(brokers, list) or not brokers:
                validation_errors.append(
                    f"{prefix}: brokers must be a non-empty list when provided"
                )
                continue
            invalid_brokers = [name for name in brokers if name not in BROKER_FUNCTIONS]
            if invalid_brokers:
                validation_errors.append(
                    f"{prefix}: invalid brokers: {', '.join(invalid_brokers)}"
                )
                continue
            selected_brokers = brokers

        selected_union.update(selected_brokers)
        normalized_orders.append(
            {
                "action": action,
                "quantity": quantity,
                "ticker": ticker.strip().upper(),
                "price": float(price) if price is not None else None,
                "selected_brokers": selected_brokers,
            }
        )

    if validation_errors:
        raise CliRuntimeError(
            "Batch order validation failed",
            ExitCode.INVALID_ARGS,
            details={"file": file_path, "validation_errors": validation_errors},
        )

    if not normalized_orders:
        raise CliRuntimeError(
            "No valid orders found in batch file",
            ExitCode.INVALID_ARGS,
            details={"file": file_path},
        )

    return normalized_orders, sorted(selected_union)


async def _run_batch_from_file(args, parser, context):
    if args.action in {"setup", "holdings", "health", "sweep"}:
        _raise_parser_error(
            parser,
            "--from-file cannot be combined with setup/holdings/health/sweep actions",
            context,
        )

    orders, brokers_to_use = _validate_batch_orders(args.from_file, parser)
    if args.broker:
        for broker_name in args.broker:
            if broker_name not in BROKER_FUNCTIONS:
                _raise_parser_error(
                    parser, f"Invalid broker specified: {broker_name}", context
                )
        brokers_to_use = args.broker
        for order in orders:
            order["selected_brokers"] = args.broker

    trade_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["trade"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS and "trade" in BROKER_FUNCTIONS[broker_name]
    }
    if context.mock_brokers:
        results = _mock_batch_results(orders)
        if context.output_format != "json":
            print(f"\nMOCK BATCH RUN: {len(orders)} order(s)")
        return ExitCode.SUCCESS, {
            "mock": True,
            "batch": True,
            "order_count": len(orders),
            "results": results,
            "messages": ["Mock mode: no live broker calls were executed"],
        }

    if context.dry_run:
        dry_run_orders = []
        total_ready = 0
        for order in orders:
            readiness, ready_brokers = _build_dry_run_readiness(order, trade_functions)
            total_ready += len(ready_brokers)
            dry_run_orders.append(
                {
                    "order": order,
                    "ready_brokers": ready_brokers,
                    "readiness": readiness,
                }
            )

        if context.output_format != "json":
            print(f"\nDRY RUN BATCH: {len(orders)} order(s)")
            for idx, entry in enumerate(dry_run_orders, start=1):
                order = entry["order"]
                print(
                    f"  [{idx}] {order['action'].upper()} {order['quantity']} {order['ticker']} @ ${order['price'] if order['price'] is not None else 'market'}"
                )
                print(f"      Ready brokers: {len(entry['ready_brokers'])}")

        exit_code = (
            ExitCode.SUCCESS if total_ready > 0 else ExitCode.CONFIG_CREDENTIAL_MISSING
        )
        return exit_code, {
            "dry_run": True,
            "batch": True,
            "order_count": len(orders),
            "orders": dry_run_orders,
        }

    try:
        await session_manager.initialize_selected_sessions(brokers_to_use)
    except Exception as exc:
        raise CliRuntimeError(
            f"Failed to initialize broker sessions: {exc}",
            ExitCode.AUTH_SESSION_FAILURE,
            details={"brokers": brokers_to_use},
        ) from exc

    # Pre-flight each order's brokers BEFORE gating; drop legs that fail
    # validation and drop any order left with no executable broker, so the
    # batch fails fast on infeasible legs instead of mid-fan-out.
    validation_skipped: list[tuple[str, str]] = []
    executable_orders = []
    for order in orders:
        order_brokers = list(order.get("selected_brokers", []))
        validate_functions = {
            b: BROKER_FUNCTIONS[b]["validate"]
            for b in order_brokers
            if b in BROKER_FUNCTIONS and "validate" in BROKER_FUNCTIONS.get(b, {})
        }
        validated, skipped = await preflight_validate(
            selected_brokers=order_brokers,
            action=order["action"],
            quantity=order["quantity"],
            ticker=order["ticker"],
            price=order.get("price"),
            validate_functions=validate_functions,
            progress_fn=None if context.output_format == "json" else print,
        )
        validation_skipped.extend(skipped)
        if validated:
            order["selected_brokers"] = validated
            executable_orders.append(order)

    if not executable_orders:
        if context.output_format != "json":
            print("All orders failed pre-flight validation; nothing to execute")
        return ExitCode.CONFIG_CREDENTIAL_MISSING, {
            "batch": True,
            "order_count": len(orders),
            "brokers": brokers_to_use,
            "results": {
                "successful": 0,
                "failed": 0,
                "skipped": len(validation_skipped),
                "statuses": [],
            },
            "validation_skipped": [
                {"broker": b, "reason": r} for b, r in validation_skipped
            ],
            "messages": [],
        }
    orders = executable_orders

    if context.output_format != "json":
        print(
            f"\nBATCH RUN: {len(orders)} order(s) across {len(brokers_to_use)} broker(s): {', '.join(brokers_to_use)}\n"
        )

    # F5 v0.3 — gate every order in the batch BEFORE order_processor fans out.
    # First-rejection aborts the batch (operators want clean fail-fast semantics
    # on batch input, not half-executed orders).
    try:
        batch_proposals = await apply_main_py_gate_batch(orders)
    except GateError as gate_err:
        raise CliRuntimeError(
            f"Batch rejected by enforcement gate ({gate_err.reason}): {gate_err}",
            ExitCode(gate_error_to_exit_code(gate_err)),
            details={
                "reason": gate_err.reason,
                "order_count": len(orders),
                "brokers": brokers_to_use,
            },
        ) from gate_err

    cli_messages = []

    def cli_response_fn(message, force_redraw=False):
        if not message:
            return
        if context.output_format == "json":
            cli_messages.append(message)
        else:
            print(message)

    # F5 v0.4 — Router-driven per-leg-token execution for the batch.
    results = await execute_via_router(
        proposals=batch_proposals,
        orders=orders,
        dry_run=False,
        progress_fn=cli_response_fn,
    )

    # Fold pre-flight skips into the aggregate skip count.
    if validation_skipped:
        results["skipped"] += len(validation_skipped)

    await record_main_py_outcome_batch(
        proposals=batch_proposals,
        orders=orders,
        results=results,
    )

    if context.output_format != "json":
        print(f"\n{'=' * 60}")
        print("🎯 Batch Results:")
        print(f"  ✅ Successful brokers: {results['successful']}")
        print(f"  ❌ Failed brokers: {results['failed']}")
        if results["skipped"] > 0:
            print(f"  ⚠️  Skipped brokers: {results['skipped']}")
        print(f"{'=' * 60}")

    return compute_trade_exit_code(results), {
        "batch": True,
        "order_count": len(orders),
        "brokers": brokers_to_use,
        "results": results,
        "validation_skipped": [
            {"broker": b, "reason": r} for b, r in validation_skipped
        ],
        "messages": cli_messages,
    }
