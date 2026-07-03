"""Buy/sell trade handler extracted from main.run_cli. Proposes and executes
the order through the ExecutionEngine (ADR 0006) — one propose path, one
execute path, shared by the CLI, TUI, operator CLI, and MCP server."""

from typing import Any

from agentic.cli_bridge import gate_error_to_exit_code
from enforcement import GateError
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
    compute_trade_exit_code,
)
from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker trade/holdings/validate functions, derived from the broker
# registry (single source of truth; ADR 0004). Kept as a module-level name so
# tests can monkeypatch it.
BROKER_FUNCTIONS = broker_functions_map()

from cli.common import (
    _mock_batch_results,
    _raise_parser_error,
    get_engine,
    render_execution_result,
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

    # Order dict retained for the mock path + envelope shape (legacy shape,
    # not consumed by the engine — propose_order takes explicit kwargs).
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

    try:
        # Initialize only the brokers we're going to use
        await session_manager.initialize_selected_sessions(brokers_to_use)
    except Exception as exc:
        raise CliRuntimeError(
            f"Failed to initialize broker sessions: {exc}",
            ExitCode.AUTH_SESSION_FAILURE,
            details={"brokers": brokers_to_use},
        ) from exc

    engine = await get_engine()

    # Pre-flight: validate each broker can take the order BEFORE proposing/
    # fan-out, so an infeasible leg (e.g. insufficient shares) fails fast
    # instead of being discovered mid-execution. Brokers that fail are
    # dropped and reported as skipped; only survivors are proposed/executed.
    validate_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["validate"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS
        and "validate" in BROKER_FUNCTIONS.get(broker_name, {})
    }
    validated, validation_skipped = await engine.validate_targets(
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

    is_rehearsal = bool(context.dry_run)

    # ADR 0006 — one propose path for every caller (CLI, TUI, operator CLI,
    # MCP). `--dry-run` is now a FULL-PIPELINE REHEARSAL: propose_order and
    # execute_order both run with dry_run=True end to end (limits, freeze
    # list, reconciliation, token minting all run; no orders are placed).
    # This replaces the old credentials-only readiness short-circuit.
    try:
        proposal = await engine.propose_order(
            ticker=args.ticker,
            qty=args.quantity,
            side=args.action,
            brokers=brokers_to_use,
            price=args.price,
            dry_run=is_rehearsal,
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

    if context.output_format != "json":
        header = (
            f"\nDRY RUN {args.action.upper()} {args.quantity} {args.ticker} @ "
            f"${args.price if args.price else 'market'}"
            if is_rehearsal
            else f"\n{args.action.upper()} {args.quantity} {args.ticker} @ "
            f"${args.price if args.price else 'market'}"
        )
        print(header)
        if is_rehearsal:
            print("DRY RUN — full pipeline rehearsal, no orders placed")
        print(
            f"Proposal: proposal_id={proposal['proposal_id']} "
            f"estimated_usd=${proposal['estimated_usd']:.2f} "
            f"leg_count={proposal['leg_count']}"
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

    if is_rehearsal:
        cli_response_fn("DRY RUN — full pipeline rehearsal, no orders placed")

    execution = await engine.execute_order(
        proposal_id=proposal["proposal_id"],
        dry_run=is_rehearsal,
    )

    # A rejection at execute time (expired proposal, dry_run mismatch,
    # proposal not found) means nothing was placed anywhere — this must NOT
    # read as success. Mirrors the old gate-rejection -> CliRuntimeError
    # translation above, using the same all-failed exit code the bridge path
    # produced when a rejection meant every selected broker failed.
    if execution.get("rejected"):
        raise CliRuntimeError(
            f"Execution rejected by enforcement gate ({execution.get('reason')}): "
            f"{execution.get('detail')}",
            ExitCode.FULL_BROKER_FAILURE,
            details={
                "reason": execution.get("reason"),
                "detail": execution.get("detail"),
                "proposal_id": proposal["proposal_id"],
                "brokers": brokers_to_use,
                "ticker": args.ticker,
                "qty": args.quantity,
                "price": args.price,
                "action": args.action,
            },
        )

    results = render_execution_result(execution)

    # Fold pre-flight skips into the reported results so the envelope reflects
    # every broker the user selected (executed + validation-skipped).
    for broker, _reason in validation_skipped:
        results["skipped"] += 1
        if results["statuses"]:
            results["statuses"][0]["skipped"].append(broker)

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
        "dry_run": is_rehearsal,
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
