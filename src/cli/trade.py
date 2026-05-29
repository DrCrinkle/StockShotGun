"""Buy/sell trade handler extracted from main.run_cli. Gates the order through
the enforcement gate, then executes it via the Router."""

from typing import Any

from agentic.cli_bridge import (
    apply_main_py_gate,
    execute_via_router,
    gate_error_to_exit_code,
    preflight_validate,
    record_main_py_outcome,
)
from enforcement import GateError
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
    compute_trade_exit_code,
)
from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from tui.broker_functions import BROKER_CONFIG as BROKER_FUNCTIONS  # type: ignore[import-untyped]

from cli.common import (
    _build_dry_run_readiness,
    _mock_batch_results,
    _raise_parser_error,
)


async def run_trade(args, parser, context) -> tuple[ExitCode, dict[str, Any]]:
    if not all([args.quantity, args.ticker]):
        _raise_parser_error(
            parser, "Quantity and ticker are required for buy/sell actions", context
        )

    # Determine which brokers to use
    if args.broker:
        # Use only the specified broker(s)
        brokers_to_use = args.broker
        # Validate that all specified brokers are available
        for broker_name in brokers_to_use:
            if broker_name not in BROKER_FUNCTIONS:
                _raise_parser_error(
                    parser, f"Invalid broker specified: {broker_name}", context
                )
    else:
        # If no broker specified, use all available brokers
        brokers_to_use = []
        for broker_name in BrokerConfig.get_all_brokers():
            if broker_name in BROKER_FUNCTIONS:
                brokers_to_use.append(broker_name)

        if not brokers_to_use:
            raise CliRuntimeError(
                "No broker credentials configured",
                ExitCode.CONFIG_CREDENTIAL_MISSING,
            )

    # Build trade functions dict for order processor
    trade_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["trade"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS and "trade" in BROKER_FUNCTIONS[broker_name]
    }
    # Create order for the processor
    order = {
        "action": args.action,
        "quantity": args.quantity,
        "ticker": args.ticker,
        "price": args.price,
        "selected_brokers": brokers_to_use,
    }

    if context.mock_brokers:
        results = _mock_batch_results([order])
        if context.output_format != "json":
            print(
                f"\nMOCK {args.action.upper()} {args.quantity} {args.ticker} @ ${args.price if args.price else 'market'}"
            )
            print("Mock mode: no live broker calls were executed")

        return ExitCode.SUCCESS, {
            "mock": True,
            "order": order,
            "results": results,
            "messages": ["Mock mode: no live broker calls were executed"],
        }

    if context.dry_run:
        readiness, ready_brokers = _build_dry_run_readiness(
            {"selected_brokers": brokers_to_use}, trade_functions
        )

        if context.output_format != "json":
            print(
                f"\nDRY RUN {args.action.upper()} {args.quantity} {args.ticker} @ ${args.price if args.price else 'market'}"
            )
            print(
                f"Preflight across {len(brokers_to_use)} broker(s): {', '.join(brokers_to_use)}"
            )
            for broker in readiness:
                status = "READY" if broker["ready"] else "NOT READY"
                print(f"  - {broker['broker']}: {status}")

        exit_code = (
            ExitCode.SUCCESS if ready_brokers else ExitCode.CONFIG_CREDENTIAL_MISSING
        )
        return exit_code, {
            "mock": context.mock_brokers,
            "dry_run": True,
            "order": order,
            "ready_brokers": ready_brokers,
            "readiness": readiness,
        }

    try:
        # Initialize only the brokers we're going to use
        await session_manager.initialize_selected_sessions(brokers_to_use)
    except Exception as exc:
        raise CliRuntimeError(
            f"Failed to initialize broker sessions: {exc}",
            ExitCode.AUTH_SESSION_FAILURE,
            details={"brokers": brokers_to_use},
        ) from exc

    # Pre-flight: validate each broker can take the order BEFORE gating/fan-out,
    # so an infeasible leg (e.g. insufficient shares) fails fast instead of
    # being discovered mid-execution. Brokers that fail are dropped and reported
    # as skipped; only survivors are gated and executed.
    validate_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["validate"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS
        and "validate" in BROKER_FUNCTIONS.get(broker_name, {})
    }
    validated, validation_skipped = await preflight_validate(
        selected_brokers=brokers_to_use,
        action=args.action,
        quantity=args.quantity,
        ticker=args.ticker,
        price=args.price,
        validate_functions=validate_functions,
        progress_fn=None if context.output_format == "json" else print,
    )
    if not validated:
        if context.output_format != "json":
            print("All brokers failed pre-flight validation; nothing to execute")
        return ExitCode.CONFIG_CREDENTIAL_MISSING, {
            "mock": context.mock_brokers,
            "order": order,
            "results": {
                "successful": 0,
                "failed": 0,
                "skipped": len(validation_skipped),
                "statuses": [
                    {
                        "ticker": args.ticker,
                        "action": args.action,
                        "successful": [],
                        "failed": [],
                        "skipped": [b for b, _ in validation_skipped],
                    }
                ],
            },
            "validation_skipped": [
                {"broker": b, "reason": r} for b, r in validation_skipped
            ],
            "messages": [],
        }
    brokers_to_use = validated
    order["selected_brokers"] = validated

    # F5 v0.2 — route through enforcement gate BEFORE order_processor fans out.
    # The gate runs: per-order limit (ISC-13), per-day limit (ISC-14), freeze
    # list (ISC-42), circuit breaker (ISC-43), and writes a propose audit entry
    # (ISC-45). A rejection here means the order MUST NOT proceed.
    try:
        gate_proposal = await apply_main_py_gate(
            action=args.action,
            quantity=args.quantity,
            ticker=args.ticker,
            price=args.price,
            brokers_to_use=brokers_to_use,
        )
    except GateError as gate_err:
        raise CliRuntimeError(
            f"Order rejected by enforcement gate ({gate_err.reason}): {gate_err}",
            ExitCode(gate_error_to_exit_code(gate_err)),
            details={
                "reason": gate_err.reason,
                "brokers": brokers_to_use,
                "ticker": args.ticker,
                "qty": args.quantity,
                "price": args.price,
                "action": args.action,
            },
        ) from gate_err

    # Use order processor for concurrent execution with better error handling
    if context.output_format != "json":
        print(
            f"\n{args.action.upper()} {args.quantity} {args.ticker} @ ${args.price if args.price else 'market'}"
        )
        print(
            f"Gate: proposal_id={gate_proposal['proposal_id']} "
            f"estimated_usd=${gate_proposal['estimated_usd']:.2f} "
            f"leg_count={gate_proposal['leg_count']}"
        )
        print(
            f"Executing across {len(brokers_to_use)} broker(s): {', '.join(brokers_to_use)}\n"
        )

    # Wrapper function for CLI mode that ignores force_redraw parameter
    cli_messages = []

    def cli_response_fn(message, force_redraw=False):
        if not message:
            return

        if context.output_format == "json":
            cli_messages.append(message)
        else:
            print(message)

    # F5 v0.4 — execute via Router (per-leg-token-validated) instead of
    # order_processor (direct broker SDK fan-out). Same broker SDKs run; the
    # gate now enforces ISC-11/12/39/40 on the legacy path too.
    results = await execute_via_router(
        proposals=[gate_proposal],
        orders=[order],
        dry_run=False,
        progress_fn=cli_response_fn,
    )

    # Fold pre-flight skips into the reported results so the envelope reflects
    # every broker the user selected (executed + validation-skipped).
    for broker, _reason in validation_skipped:
        results["skipped"] += 1
        if results["statuses"]:
            results["statuses"][0]["skipped"].append(broker)

    await record_main_py_outcome(
        proposal_id=gate_proposal["proposal_id"],
        action=args.action,
        quantity=args.quantity,
        ticker=args.ticker,
        price=args.price,
        results=results,
    )

    # Print summary
    if context.output_format != "json":
        print(f"\n{'=' * 60}")
        print("🎯 Total Results:")
        print(f"  ✅ Successful brokers: {results['successful']}")
        print(f"  ❌ Failed brokers: {results['failed']}")
        if results["skipped"] > 0:
            print(f"  ⚠️  Skipped brokers: {results['skipped']}")
        print(f"{'=' * 60}")

    return compute_trade_exit_code(results), {
        "mock": context.mock_brokers,
        "order": {
            "action": args.action,
            "quantity": args.quantity,
            "ticker": args.ticker,
            "price": args.price,
            "selected_brokers": brokers_to_use,
        },
        "results": results,
        "validation_skipped": [
            {"broker": b, "reason": r} for b, r in validation_skipped
        ],
        "messages": cli_messages,
    }
