"""`automate` command handler: ingest a chat recap, derive due buy/sell orders,
propose and execute them through the ExecutionEngine (ADR 0006)."""

import json
from datetime import datetime
from typing import Any

from enforcement import GateError
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
    compute_trade_exit_code,
)
from automation_recap import AutomationRecapStore, parse_chat_recap  # type: ignore[import-untyped]
from brokers import session_manager  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()

from cli.common import (
    _default_brokers_for_trade,
    _mock_batch_results,
    _raise_parser_error,
    aggregate_execution_results,
    gate_error_to_exit_code,
    get_engine,
    render_execution_result,
)
from cli.sweep import _resolve_today_date, _sum_holdings_quantity


async def _run_automate_from_recap(args, parser, context):
    if not args.recap_file:
        _raise_parser_error(
            parser, "--recap-file is required for automate action", context
        )

    try:
        with open(args.recap_file, "r", encoding="utf-8") as recap_handle:
            recap_text = recap_handle.read()
    except FileNotFoundError as exc:
        raise CliRuntimeError(
            f"Recap file not found: {args.recap_file}",
            ExitCode.INVALID_ARGS,
            details={"recap_file": args.recap_file},
        ) from exc

    upcoming, stock_back = parse_chat_recap(recap_text)
    store = AutomationRecapStore(args.db_path)

    try:
        now = datetime.now()
        try:
            today_date = _resolve_today_date(now, args.today_mmdd)
        except ValueError as exc:
            _raise_parser_error(parser, str(exc), context)
        today_mmdd = today_date.strftime("%m/%d")
        ingestion = store.record_recap(recap_text, upcoming, stock_back, now)

        due_buys = store.get_due_buy_signals(today_date)
        pending_sells = store.get_pending_sell_triggers()
        available_brokers = _default_brokers_for_trade()

        if args.broker:
            for broker_name in args.broker:
                if broker_name not in BROKER_FUNCTIONS:
                    _raise_parser_error(
                        parser, f"Invalid broker specified: {broker_name}", context
                    )
            buy_brokers = args.broker
        else:
            buy_brokers = available_brokers

        orders = []
        order_sources: list[dict[str, Any]] = []

        for signal in due_buys:
            if not buy_brokers:
                continue
            orders.append(
                {
                    "action": "buy",
                    "quantity": max(1, args.default_qty),
                    "ticker": signal["ticker"],
                    "price": None,
                    "selected_brokers": buy_brokers,
                }
            )
            order_sources.append(
                {
                    "type": "buy",
                    "id": int(signal["id"]),
                    "expected_brokers": list(buy_brokers),
                }
            )

        for trigger in pending_sells:
            trigger_brokers = json.loads(trigger["brokers_json"])
            if args.broker:
                selected_brokers = args.broker
            elif trigger_brokers:
                selected_brokers = [
                    b
                    for b in trigger_brokers
                    if b in BROKER_FUNCTIONS and b in available_brokers
                ]
            else:
                selected_brokers = available_brokers

            if not selected_brokers:
                continue

            if context.mock_brokers or context.dry_run:
                for broker_name in selected_brokers:
                    orders.append(
                        {
                            "action": "sell",
                            "quantity": max(1, args.default_qty),
                            "ticker": trigger["ticker"],
                            "price": None,
                            "selected_brokers": [broker_name],
                        }
                    )
                    order_sources.append(
                        {
                            "type": "sell",
                            "id": int(trigger["id"]),
                            "expected_brokers": [broker_name],
                        }
                    )
                continue

            try:
                await session_manager.initialize_selected_sessions(selected_brokers)
            except Exception as exc:
                raise CliRuntimeError(
                    f"Failed to initialize sessions for automated sells: {exc}",
                    ExitCode.AUTH_SESSION_FAILURE,
                ) from exc

            for broker_name in selected_brokers:
                holdings_fn = BROKER_FUNCTIONS[broker_name]["holdings"]
                try:
                    holdings = await holdings_fn(trigger["ticker"])
                except Exception as exc:
                    print(
                        f"⚠ Holdings lookup failed for {trigger['ticker']} on {broker_name}: {exc}"
                    )
                    holdings = None
                quantity = _sum_holdings_quantity(holdings)
                if quantity <= 0:
                    continue
                orders.append(
                    {
                        "action": "sell",
                        "quantity": quantity,
                        "ticker": trigger["ticker"],
                        "price": None,
                        "selected_brokers": [broker_name],
                    }
                )
                order_sources.append(
                    {
                        "type": "sell",
                        "id": int(trigger["id"]),
                        "expected_brokers": [broker_name],
                    }
                )

        if not orders:
            return ExitCode.SUCCESS, {
                "automation": True,
                "message": "No due actions generated from recap",
                "ingestion": ingestion,
                "today_mmdd": today_mmdd,
                "generated_orders": 0,
            }

        if context.mock_brokers:
            mock_results = _mock_batch_results(orders)
            return ExitCode.SUCCESS, {
                "automation": True,
                "mock": True,
                "ingestion": ingestion,
                "today_mmdd": today_mmdd,
                "generated_orders": len(orders),
                "results": mock_results,
            }

        is_rehearsal = bool(context.dry_run)

        brokers_to_initialize = sorted(
            {
                broker
                for order in orders
                for broker in order["selected_brokers"]
                if broker in BROKER_FUNCTIONS
            }
        )
        try:
            await session_manager.initialize_selected_sessions(brokers_to_initialize)
        except Exception as exc:
            raise CliRuntimeError(
                f"Failed to initialize broker sessions: {exc}",
                ExitCode.AUTH_SESSION_FAILURE,
            ) from exc

        engine = await get_engine()

        # Pre-flight each generated order BEFORE proposing; drop legs that
        # fail validation and any order left with no executable broker. Keep
        # `orders` and `order_sources` index-aligned so the post-execution
        # buy/sell attribution below stays correct.
        validation_skipped: list[tuple[str, str]] = []
        executable: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for order, source in zip(orders, order_sources):
            order_brokers = list(order.get("selected_brokers", []))
            validate_functions = {
                b: BROKER_FUNCTIONS[b]["validate"]
                for b in order_brokers
                if b in BROKER_FUNCTIONS and "validate" in BROKER_FUNCTIONS.get(b, {})
            }
            validated, skipped = await engine.validate_targets(
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
                executable.append((order, source))

        if not executable:
            return ExitCode.CONFIG_CREDENTIAL_MISSING, {
                "automation": True,
                "ingestion": ingestion,
                "today_mmdd": today_mmdd,
                "generated_orders": len(orders),
                "executed_buy_signals": [],
                "executed_sell_triggers": [],
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
        orders = [o for o, _ in executable]
        order_sources = [s for _, s in executable]

        automation_messages = []

        def automation_response_fn(message, force_redraw=False):
            if not message:
                return
            if context.output_format == "json":
                automation_messages.append(message)
            else:
                print(message)

        if is_rehearsal:
            automation_response_fn(
                "DRY RUN — full pipeline rehearsal, no orders placed"
            )

        # ADR 0006 — one propose path for every caller. Two phases, mirroring
        # `apply_main_py_gate_batch` -> `execute_via_router`:
        #
        #   Phase 1 — propose EVERY generated order first. The first
        #   GateError raises and aborts the whole automation batch BEFORE any
        #   order is executed (load-bearing: gating every order ahead of any
        #   fan-out is what `apply_main_py_gate_batch` did).
        #
        #   Phase 2 — execute every gated proposal in order. `--dry-run` is a
        #   full-pipeline rehearsal — propose and execute both run with
        #   dry_run=True end to end.
        automation_proposals: list[dict[str, Any]] = []
        for order in orders:
            try:
                proposal = await engine.propose_order(
                    ticker=str(order["ticker"]),
                    qty=order["quantity"],
                    side=str(order["action"]),
                    brokers=list(order["selected_brokers"]),
                    price=order.get("price"),
                    dry_run=is_rehearsal,
                )
            except GateError as gate_err:
                raise CliRuntimeError(
                    f"Automation batch rejected by enforcement gate "
                    f"({gate_err.reason}): {gate_err}",
                    ExitCode(gate_error_to_exit_code(gate_err)),
                    details={
                        "reason": gate_err.reason,
                        "order_count": len(orders),
                        "automation": True,
                        "ticker": order["ticker"],
                        "qty": order["quantity"],
                        "action": order["action"],
                    },
                ) from gate_err
            automation_proposals.append(proposal)

        # A sell trigger may be split across multiple orders (one per
        # broker — see the pending_sells loop above), so the full expected
        # broker set per source is only knowable by scanning ALL sources
        # up front. Buy signals are always exactly one order = one source.
        # Computing this before the execute loop lets us mark each source
        # incrementally, immediately after the order(s) that complete it
        # finish executing — not in bulk after the whole batch.
        expected_brokers_by_source: dict[tuple[str, int], set[str]] = {}
        for source in order_sources:
            source_key = (source["type"], source["id"])
            expected_brokers_by_source.setdefault(source_key, set()).update(
                source["expected_brokers"]
            )
        completed_brokers_by_source: dict[tuple[str, int], set[str]] = {}
        marked_sources: set[tuple[str, int]] = set()
        successful_buy_ids: set[int] = set()
        successful_sell_ids: set[int] = set()

        rendered_results = []
        for order, proposal, source in zip(orders, automation_proposals, order_sources):
            ticker = str(order["ticker"])
            action = str(order["action"])
            qty = order["quantity"]
            order_brokers = list(order["selected_brokers"])

            automation_response_fn(
                f"[automate] executing proposal {proposal['proposal_id'][:12]}… "
                f"for {action} {qty} {ticker} ({proposal['leg_count']} leg(s))"
            )

            execution = await engine.execute_order(
                proposal_id=proposal["proposal_id"],
                dry_run=is_rehearsal,
            )

            # A rejection at execute time means nothing was placed anywhere
            # for this order — this must NOT read as success. Orders that
            # already executed earlier in this loop are not lost: their
            # signals were already marked executed immediately after they
            # completed (below), and their rendered results ride along in
            # `details` so JSON error output isn't silently missing
            # completed work.
            if execution.get("rejected"):
                raise CliRuntimeError(
                    f"Execution rejected by enforcement gate "
                    f"({execution.get('reason')}): {execution.get('detail')}",
                    ExitCode.FULL_BROKER_FAILURE,
                    details={
                        "reason": execution.get("reason"),
                        "detail": execution.get("detail"),
                        "proposal_id": proposal["proposal_id"],
                        "brokers": order_brokers,
                        "ticker": ticker,
                        "qty": qty,
                        "action": action,
                        "completed_results": aggregate_execution_results(
                            rendered_results
                        ),
                        "completed_orders": len(rendered_results),
                    },
                )

            rendered = render_execution_result(execution)
            for status in rendered["statuses"]:
                for broker in status["successful"]:
                    automation_response_fn(f"[automate] ✓ {broker}: placed")
                for broker in status["failed"]:
                    automation_response_fn(f"[automate] ✗ {broker}: failed")
            rendered_results.append(rendered)

            # Mark this order's signal(s) executed IMMEDIATELY — a mid-batch
            # abort on a later order must not leave an already-filled
            # order's signal in 'pending' (it would re-execute — a
            # double-trade — on the next automate run). Dry-run rehearsals
            # place nothing anywhere, so they must never mark anything.
            if is_rehearsal:
                continue

            source_key = (source["type"], source["id"])
            for status in rendered["statuses"]:
                completed_brokers_by_source.setdefault(source_key, set()).update(
                    status.get("successful", [])
                )

            expected_brokers = expected_brokers_by_source.get(source_key, set())
            if not expected_brokers or source_key in marked_sources:
                continue
            completed_brokers = completed_brokers_by_source.get(source_key, set())
            if completed_brokers != expected_brokers:
                continue

            source_type, source_id = source_key
            if source_type == "buy":
                store.mark_buy_signals_executed([source_id], now)
                successful_buy_ids.add(source_id)
            elif source_type == "sell":
                store.mark_sell_triggers_executed([source_id], now)
                successful_sell_ids.add(source_id)
            marked_sources.add(source_key)

        results = aggregate_execution_results(rendered_results)

        # Fold pre-flight skips into the aggregate skip count.
        if validation_skipped:
            results["skipped"] += len(validation_skipped)

        return compute_trade_exit_code(results), {
            "automation": True,
            "dry_run": is_rehearsal,
            "ingestion": ingestion,
            "today_mmdd": today_mmdd,
            "generated_orders": len(orders),
            "executed_buy_signals": sorted(successful_buy_ids),
            "executed_sell_triggers": sorted(successful_sell_ids),
            "results": results,
            "validation_skipped": [
                {"broker": b, "reason": r} for b, r in validation_skipped
            ],
            "messages": automation_messages,
        }
    finally:
        store.close()
