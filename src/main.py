import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import warnings
from dataclasses import asdict
from datetime import datetime
from typing import Any, NoReturn, cast
from setup import setup  # type: ignore[import-untyped]
from tui import run_tui  # type: ignore[import-untyped]
from tui.input_handler import (  # type: ignore[import-untyped]
    restore_original_input,
    set_non_interactive_mode,
    setup_tui_input_interception,
)
from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from tui.broker_functions import BROKER_CONFIG as BROKER_FUNCTIONS  # type: ignore[import-untyped]
from order_processor import order_processor  # type: ignore[import-untyped]
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExecutionContext,
    ExitCode,
    build_response_envelope,
    compute_trade_exit_code,
)
from automation_recap import AutomationRecapStore, parse_chat_recap  # type: ignore[import-untyped]
from sweep import (  # type: ignore[import-untyped]
    SweepResult,
    SweepStatus,
    parse_ratio,
    status_summary,
    sweep_all_brokers,
)

# Suppress requests library warning about chardet version
warnings.filterwarnings(
    "ignore",
    message="urllib3.*or chardet.*doesn't match a supported version",
    category=UserWarning,
)

def _json_requested_from_argv(argv):
    return _extract_option_value(argv, "--output").lower() == "json"


def _extract_option_value(argv, option_name):
    if option_name not in argv:
        return ""
    idx = argv.index(option_name)
    if idx + 1 < len(argv):
        return argv[idx + 1]
    return ""


def _extract_action_from_argv(argv):
    known_actions = {"buy", "sell", "setup", "holdings", "health", "automate", "sweep"}
    for token in argv:
        if token in known_actions:
            return token
    return None


class RuntimeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        if _json_requested_from_argv(sys.argv[1:]):
            request_id = _extract_option_value(sys.argv[1:], "--request-id")
            if not request_id:
                request_id = ExecutionContext(command=None).request_id
            response = build_response_envelope(
                ok=False,
                command=_extract_action_from_argv(sys.argv[1:]),
                request_id=request_id,
                errors=[
                    {
                        "message": f"{self.prog}: error: {message}",
                        "exit_code": int(ExitCode.INVALID_ARGS),
                        "details": {},
                    }
                ],
            )
            print(json.dumps(response))
            raise SystemExit(int(ExitCode.INVALID_ARGS))

        super().error(message)


async def print_holdings(holdings):
    """Print holdings in a formatted way."""
    if holdings:
        for account, positions in holdings.items():
            profile_name = ""
            account_id = str(account)
            if ":" in account_id:
                profile_name, account_id = account_id.split(":", 1)

            if profile_name:
                print(f"\nAccount: {account_id} (Profile: {profile_name})")
            else:
                print(f"\nAccount: {account_id}")
            if not positions:
                print("No positions found")
            for pos in positions:
                symbol = pos.get("symbol", "N/A")
                quantity = pos.get("quantity", 0)

                cost_basis = pos.get("cost_basis")
                if cost_basis is None:
                    cost_basis_display = "N/A"
                else:
                    cost_basis_display = f"${float(cost_basis):.2f}"

                current_value = pos.get("current_value")
                if current_value is None:
                    fallback_value = pos.get("value")
                    if fallback_value is None and pos.get("price") is not None:
                        try:
                            fallback_value = float(pos["price"]) * float(quantity)
                        except (TypeError, ValueError):
                            fallback_value = None
                    current_value = fallback_value

                if current_value is None:
                    current_value_display = "N/A"
                else:
                    current_value_display = f"${float(current_value):.2f}"

                print(
                    f"\nSymbol: {symbol}\n"
                    f"Quantity: {quantity}\n"
                    f"Cost Basis: {cost_basis_display}\n"
                    f"Current Value: {current_value_display}"
                )


def _raise_parser_error(parser, message, context) -> NoReturn:
    if context.output_format != "json":
        parser.print_usage(sys.stderr)
    raise CliRuntimeError(
        f"{parser.prog}: error: {message}",
        ExitCode.INVALID_ARGS,
    )


def _emit_runtime_error(error, context):
    if context.output_format == "json":
        response = build_response_envelope(
            ok=False,
            command=context.command,
            request_id=context.request_id,
            errors=[
                {
                    "message": error.message,
                    "exit_code": int(error.exit_code),
                    "details": error.details or {},
                }
            ],
        )
        print(json.dumps(response))
        return

    print(str(error), file=sys.stderr)


def _emit_runtime_success(context, data):
    if context.output_format != "json":
        return

    response = build_response_envelope(
        ok=True,
        command=context.command,
        request_id=context.request_id,
        data=data,
    )
    print(json.dumps(response))


def _emit_log_event(context, level, event, details=None):
    if context.log_format != "jsonl":
        return

    log_record = {
        "request_id": context.request_id,
        "command": context.command,
        "level": level,
        "event": event,
        "details": details or {},
    }
    serialized = json.dumps(log_record)

    if context.log_file:
        with open(context.log_file, "a", encoding="utf-8") as log_handle:
            log_handle.write(serialized + "\n")
    else:
        print(serialized, file=sys.stderr)


def _default_brokers_for_trade():
    brokers = []
    for broker_name in BrokerConfig.get_all_brokers():
        if broker_name in BROKER_FUNCTIONS and "trade" in BROKER_FUNCTIONS[broker_name]:
            brokers.append(broker_name)
    return brokers


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


def _credentials_present_for_broker(broker_name: str) -> bool:
    if broker_name == "Webull":
        webull_profiles = os.getenv("WEBULL_PROFILES")
        if webull_profiles:
            return True

    required_env_vars = BrokerConfig.get_env_vars(broker_name)
    return all(os.getenv(var) for var in required_env_vars)


def _missing_env_vars_for_broker(broker_name: str) -> list[str]:
    if broker_name == "Webull":
        webull_profiles = os.getenv("WEBULL_PROFILES")
        if webull_profiles:
            return []

    required_env_vars = BrokerConfig.get_env_vars(broker_name)
    return [var for var in required_env_vars if not os.getenv(var)]


async def _collect_webull_health_details(context: ExecutionContext) -> dict[str, Any]:
    details: dict[str, Any] = {
        "profiles_configured": 0,
        "token_ready_profiles": 0,
        "profiles_initialized": 0,
        "accounts_discovered": 0,
        "init_error": "",
    }

    raw_profiles = os.getenv("WEBULL_PROFILES", "").strip()
    if raw_profiles:
        try:
            parsed = json.loads(raw_profiles)
            if isinstance(parsed, dict):
                parsed = parsed.get("profiles", [])
            if isinstance(parsed, list):
                details["profiles_configured"] = len(
                    [p for p in parsed if isinstance(p, dict)]
                )
                token_ready = 0
                for profile in parsed:
                    if not isinstance(profile, dict):
                        continue
                    if all(
                        [
                            str(profile.get("access_token", "")).strip(),
                            str(profile.get("refresh_token", "")).strip(),
                            str(profile.get("uuid", "")).strip(),
                        ]
                    ):
                        token_ready += 1
                details["token_ready_profiles"] = token_ready
        except (TypeError, ValueError, json.JSONDecodeError):
            details["init_error"] = "WEBULL_PROFILES is not valid JSON"
    else:
        details["profiles_configured"] = (
            1
            if all(
                [
                    os.getenv("WEBULL_ACCESS_TOKEN", "").strip(),
                    os.getenv("WEBULL_REFRESH_TOKEN", "").strip(),
                    os.getenv("WEBULL_UUID", "").strip(),
                ]
            )
            else 0
        )
        details["token_ready_profiles"] = details["profiles_configured"]

    if context.mock_brokers:
        return details

    try:
        if session_manager.sessions.get("webull") is None:
            sink = io.StringIO()
            with contextlib.redirect_stdout(sink):
                await session_manager.initialize_selected_sessions(["Webull"])

        webull_session = session_manager.sessions.get("webull")
        if not webull_session:
            if not details["init_error"]:
                details["init_error"] = "Webull session not initialized"
            return details

        profiles = webull_session.get("profiles") or []
        details["profiles_initialized"] = len(profiles)
        details["accounts_discovered"] = sum(
            len(profile.get("accounts", []))
            for profile in profiles
            if isinstance(profile, dict)
        )
    except Exception as exc:
        details["init_error"] = str(exc)

    return details


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


def _today_mmdd(today_override: str) -> str:
    if today_override:
        return today_override
    return datetime.now().strftime("%m/%d")


def _resolve_today_date(reference_now: datetime, today_override: str):
    if not today_override:
        return reference_now.date()

    try:
        parsed_today = datetime.strptime(today_override, "%m/%d")
    except ValueError as exc:
        raise ValueError("today override must use MM/DD format") from exc

    return reference_now.replace(month=parsed_today.month, day=parsed_today.day).date()


def _sum_holdings_quantity(holdings: dict[str, Any] | None) -> int:
    if not holdings:
        return 0
    total = 0
    for positions in holdings.values():
        if not positions:
            continue
        for pos in positions:
            quantity = pos.get("quantity", 0)
            try:
                quantity_value = int(float(quantity))
            except (TypeError, ValueError):
                quantity_value = 0
            total += max(0, quantity_value)
    return total


def _mock_sweep_holdings_fn(broker_name: str, ticker: str):
    async def mock_holdings(requested_ticker: str):
        if requested_ticker.upper().endswith("D"):
            return {}

        symbol = ticker.upper()
        samples: dict[str, Any] = {
            "Robinhood": {"MOCK-RH": [{"symbol": symbol, "quantity": 0.04}]},
            "TastyTrade": {"MOCK-TT": [{"symbol": symbol, "quantity": 0.04}]},
            "BBAE": {"MOCK-BBAE": []},
            "DSPAC": {"MOCK-DSPAC": []},
            "Firstrade": {"MOCK-FT": []},
            "Public": {"MOCK-PUBLIC": [{"symbol": symbol, "quantity": 1}]},
            "SoFi": {"MOCK-SOFI": []},
            "Webull": {"MOCK-WEBULL": []},
            "Schwab": {"MOCK-SCHWAB": []},
            "WellsFargo": {"MOCK-WF": []},
            "Chase": {"MOCK-CHASE": []},
            "Fennel": {"MOCK-FENNEL": [{"symbol": symbol, "quantity": 1}]},
            "Tradier": None,
        }
        return samples.get(broker_name, {})

    return mock_holdings


def _sweep_result_to_dict(result: SweepResult) -> dict[str, Any]:
    return {
        "broker": result.broker,
        "account_id": result.account_id,
        "holdings_outcome": result.holdings_outcome.value,
        "status": result.status.value,
        "observed_qty": result.observed_qty,
        "expected_post_qty": result.expected_post_qty,
        "pre_split_qty": result.pre_split_qty,
        "profile": asdict(result.profile),
        "details": result.details,
    }


def _format_qty(quantity: float | None) -> str:
    if quantity is None:
        return "---"
    if quantity.is_integer():
        return str(int(quantity))
    return f"{quantity:g}"


def _print_sweep_results(
    ticker: str,
    ratio: str,
    pre_split_qty: int,
    results: list[SweepResult],
    force: bool,
) -> None:
    print(
        f"\nSweep results for {ticker.upper()} "
        f"(ratio {ratio}, pre-split qty: {pre_split_qty}):\n"
    )
    for result in results:
        status = result.status.name
        if result.status == SweepStatus.AMBIGUOUS and force:
            detail = "ambiguous; included by --force"
        else:
            detail = result.details.splitlines()[0]
        print(
            f"  {result.broker:<12} [{result.account_id:<16}] "
            f"{status:<20} qty={_format_qty(result.observed_qty):<6} {detail}"
        )

    summary = status_summary(results)
    print(
        "\nSummary: "
        f"{summary['share_arrived']} arrived, "
        f"{summary['processing']} processing, "
        f"{summary['fractional_pending']} fractional, "
        f"{summary['awaiting_split']} awaiting split, "
        f"{summary['ambiguous']} ambiguous, "
        f"{summary['skipped']} skipped, "
        f"{summary['error']} error"
    )


async def _run_sweep(args, parser, context):
    try:
        parse_ratio(args.ratio)
    except ValueError as exc:
        _raise_parser_error(parser, str(exc), context)

    if args.pre_qty < 0:
        _raise_parser_error(parser, "--pre-qty cannot be negative", context)

    if args.broker:
        selected_brokers = args.broker
        for broker_name in selected_brokers:
            if broker_name not in BROKER_FUNCTIONS:
                _raise_parser_error(
                    parser, f"Invalid broker specified: {broker_name}", context
                )
    else:
        selected_brokers = [
            broker_name
            for broker_name in BrokerConfig.get_all_brokers()
            if broker_name in BROKER_FUNCTIONS and "holdings" in BROKER_FUNCTIONS[broker_name]
        ]

    if not selected_brokers:
        raise CliRuntimeError(
            "No broker holdings functions configured",
            ExitCode.CONFIG_CREDENTIAL_MISSING,
        )

    if context.mock_brokers:
        broker_holdings = {
            broker_name: _mock_sweep_holdings_fn(broker_name, args.ticker)
            for broker_name in selected_brokers
        }
    else:
        try:
            await session_manager.initialize_selected_sessions(selected_brokers)
        except Exception as exc:
            raise CliRuntimeError(
                f"Failed to initialize broker sessions: {exc}",
                ExitCode.AUTH_SESSION_FAILURE,
                details={"brokers": selected_brokers},
            ) from exc

        broker_holdings = {
            broker_name: BROKER_FUNCTIONS[broker_name]["holdings"]
            for broker_name in selected_brokers
            if broker_name in BROKER_FUNCTIONS
            and "holdings" in BROKER_FUNCTIONS[broker_name]
        }

    results = await sweep_all_brokers(
        args.ticker.upper(),
        args.ratio,
        args.pre_qty,
        broker_holdings,
        selected_brokers,
    )
    summary = status_summary(results)

    if context.output_format != "json":
        _print_sweep_results(args.ticker, args.ratio, args.pre_qty, results, args.force)

    sellable_statuses = {SweepStatus.SHARE_ARRIVED}
    if args.force:
        sellable_statuses.add(SweepStatus.AMBIGUOUS)
    sellable_results = [
        result for result in results if result.status in sellable_statuses
    ]

    all_skipped_or_error = results and all(
        result.status in {SweepStatus.SKIPPED, SweepStatus.ERROR} for result in results
    )
    if all_skipped_or_error:
        exit_code = ExitCode.CONFIG_CREDENTIAL_MISSING
    else:
        exit_code = ExitCode.SUCCESS

    return exit_code, {
        "mock": context.mock_brokers,
        "ticker": args.ticker.upper(),
        "ratio": args.ratio,
        "pre_split_qty": args.pre_qty,
        "force": args.force,
        "selected_brokers": selected_brokers,
        "results": [_sweep_result_to_dict(result) for result in results],
        "summary": summary,
        "sellable": [_sweep_result_to_dict(result) for result in sellable_results],
    }


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
    validate_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["validate"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS
        and "validate" in BROKER_FUNCTIONS.get(broker_name, {})
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

    if context.output_format != "json":
        print(
            f"\nBATCH RUN: {len(orders)} order(s) across {len(brokers_to_use)} broker(s): {', '.join(brokers_to_use)}\n"
        )

    cli_messages = []

    def cli_response_fn(message, force_redraw=False):
        if not message:
            return
        if context.output_format == "json":
            cli_messages.append(message)
        else:
            print(message)

    results = await order_processor.process_orders(
        orders,
        trade_functions,
        cli_response_fn,
        validate_functions=validate_functions,
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
        "messages": cli_messages,
    }


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

        trade_functions = {
            broker_name: BROKER_FUNCTIONS[broker_name]["trade"]
            for broker_name in available_brokers
            if broker_name in BROKER_FUNCTIONS
            and "trade" in BROKER_FUNCTIONS[broker_name]
        }
        validate_functions = {
            broker_name: BROKER_FUNCTIONS[broker_name]["validate"]
            for broker_name in available_brokers
            if broker_name in BROKER_FUNCTIONS
            and "validate" in BROKER_FUNCTIONS.get(broker_name, {})
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

        if context.dry_run:
            dry_run_orders = []
            total_ready = 0
            for order in orders:
                readiness, ready_brokers = _build_dry_run_readiness(
                    order, trade_functions
                )
                total_ready += len(ready_brokers)
                dry_run_orders.append(
                    {
                        "order": order,
                        "ready_brokers": ready_brokers,
                        "readiness": readiness,
                    }
                )
            exit_code = (
                ExitCode.SUCCESS
                if total_ready > 0
                else ExitCode.CONFIG_CREDENTIAL_MISSING
            )
            return exit_code, {
                "automation": True,
                "dry_run": True,
                "ingestion": ingestion,
                "today_mmdd": today_mmdd,
                "generated_orders": len(orders),
                "orders": dry_run_orders,
            }

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

        automation_messages = []

        def automation_response_fn(message, force_redraw=False):
            if not message:
                return
            if context.output_format == "json":
                automation_messages.append(message)
            else:
                print(message)

        results = await order_processor.process_orders(
            orders,
            trade_functions,
            automation_response_fn,
            validate_functions=validate_functions,
        )

        successful_buy_ids = set()
        successful_sell_ids = set()
        completed_brokers_by_source: dict[tuple[str, int], set[str]] = {}
        expected_brokers_by_source: dict[tuple[str, int], set[str]] = {}
        for idx, status in enumerate(results.get("statuses", [])):
            if idx >= len(order_sources):
                continue
            source = order_sources[idx]
            source_key = (source["type"], source["id"])
            expected_brokers_by_source.setdefault(source_key, set()).update(
                source["expected_brokers"]
            )
            completed_brokers_by_source.setdefault(source_key, set()).update(
                status.get("successful", [])
            )

        for source_key, expected_brokers in expected_brokers_by_source.items():
            if not expected_brokers:
                continue
            completed_brokers = completed_brokers_by_source.get(source_key, set())
            if completed_brokers != expected_brokers:
                continue

            source_type, source_id = source_key
            if source_type == "buy":
                successful_buy_ids.add(source_id)
            if source_type == "sell":
                successful_sell_ids.add(source_id)

        store.mark_buy_signals_executed(sorted(successful_buy_ids), now)
        store.mark_sell_triggers_executed(sorted(successful_sell_ids), now)

        return compute_trade_exit_code(results), {
            "automation": True,
            "ingestion": ingestion,
            "today_mmdd": today_mmdd,
            "generated_orders": len(orders),
            "executed_buy_signals": sorted(successful_buy_ids),
            "executed_sell_triggers": sorted(successful_sell_ids),
            "results": results,
            "messages": automation_messages,
        }
    finally:
        store.close()


async def run_cli(args, parser, context) -> tuple[ExitCode, dict[str, Any]]:
    if args.action == "automate":
        return await _run_automate_from_recap(args, parser, context)

    if args.action == "sweep":
        return await _run_sweep(args, parser, context)

    if args.from_file:
        return await _run_batch_from_file(args, parser, context)

    if args.action == "setup":
        if context.non_interactive:
            raise CliRuntimeError(
                "setup requires interactive input; rerun without --non-interactive",
                ExitCode.NON_INTERACTIVE_INPUT_REQUIRED,
            )

        setup_logs = []
        if context.output_format == "json":
            setup_out = io.StringIO()
            with contextlib.redirect_stdout(setup_out):
                setup(
                    non_interactive=context.non_interactive, broker_filter=args.broker
                )
            setup_logs = [
                line for line in setup_out.getvalue().splitlines() if line.strip()
            ]
        else:
            setup(non_interactive=context.non_interactive, broker_filter=args.broker)
            print(
                "Credentials setup complete. Please rerun the script with trade details."
            )

        return ExitCode.SUCCESS, {
            "message": "Credentials setup complete. Please rerun the script with trade details.",
            "logs": setup_logs,
        }

    if args.action == "health":
        if args.broker:
            brokers_to_check = args.broker
            for broker_name in brokers_to_check:
                if broker_name not in BrokerConfig.BROKERS:
                    _raise_parser_error(
                        parser, f"Invalid broker specified: {broker_name}", context
                    )
        else:
            brokers_to_check = BrokerConfig.get_all_brokers()

        broker_health = []
        ready_count = 0
        for broker_name in brokers_to_check:
            missing_env_vars = _missing_env_vars_for_broker(broker_name)
            credentials_present = _credentials_present_for_broker(broker_name)
            broker_details: dict[str, Any] = {}

            session_key = BrokerConfig.get_session_key(broker_name)
            session_initialized = bool(
                session_key and session_manager.sessions.get(session_key) is not None
            )
            has_trade = (
                broker_name in BROKER_FUNCTIONS
                and "trade" in BROKER_FUNCTIONS[broker_name]
            )
            has_holdings = (
                broker_name in BROKER_FUNCTIONS
                and "holdings" in BROKER_FUNCTIONS[broker_name]
            )
            ready = credentials_present and (has_trade or has_holdings)

            if broker_name == "Webull" and credentials_present:
                broker_details = await _collect_webull_health_details(context)
                initialized_profiles = int(
                    broker_details.get("profiles_initialized", 0) or 0
                )
                discovered_accounts = int(
                    broker_details.get("accounts_discovered", 0) or 0
                )
                if initialized_profiles == 0 or discovered_accounts == 0:
                    ready = False

            if context.mock_brokers:
                missing_env_vars = []
                credentials_present = True
                session_initialized = True
                ready = True
            if ready:
                ready_count += 1

            broker_health.append(
                {
                    "broker": broker_name,
                    "ready": ready,
                    "credentials_present": credentials_present,
                    "missing_env_vars": missing_env_vars,
                    "session_initialized": session_initialized,
                    "has_trade": has_trade,
                    "has_holdings": has_holdings,
                    "details": broker_details,
                }
            )

        if context.output_format != "json":
            print("\nBroker Health")
            print("=" * 60)
            for item in broker_health:
                status = "READY" if item["ready"] else "NOT READY"
                print(f"- {item['broker']}: {status}")
                if item["missing_env_vars"]:
                    print(
                        f"  missing credentials: {', '.join(item['missing_env_vars'])}"
                    )
                details = item.get("details") or {}
                if item["broker"] == "Webull" and details:
                    print(
                        "  profiles: "
                        f"configured={details.get('profiles_configured', 0)}, "
                        f"token-ready={details.get('token_ready_profiles', 0)}, "
                        f"initialized={details.get('profiles_initialized', 0)}"
                    )
                    print(
                        f"  accounts discovered: {details.get('accounts_discovered', 0)}"
                    )
                    init_error = details.get("init_error")
                    if init_error:
                        print(f"  init error: {init_error}")
            print("=" * 60)
            print(f"Ready brokers: {ready_count}/{len(broker_health)}")

        exit_code = (
            ExitCode.SUCCESS if ready_count > 0 else ExitCode.CONFIG_CREDENTIAL_MISSING
        )
        return exit_code, {
            "mock": context.mock_brokers,
            "health": broker_health,
            "ready_brokers": ready_count,
            "total_brokers": len(broker_health),
        }

    if args.action == "holdings":
        if not args.broker:
            _raise_parser_error(
                parser, "--broker is required for holdings action", context
            )
        broker = args.broker[0]  # For holdings, use the first specified broker
        if broker not in BROKER_FUNCTIONS:
            _raise_parser_error(
                parser, "Invalid broker specified for holdings", context
            )

        if context.mock_brokers:
            holdings = {
                "MOCK-ACCOUNT": [
                    {
                        "symbol": args.ticker or "MOCK",
                        "quantity": 100,
                        "cost_basis": 10.0,
                        "current_value": 1200.0,
                    }
                ]
            }
        else:
            try:
                # Initialize only the selected broker
                await session_manager.initialize_selected_sessions([broker])
                holdings_func = BROKER_FUNCTIONS[broker]["holdings"]
                holdings = await holdings_func(args.ticker)
            except Exception as exc:
                raise CliRuntimeError(
                    f"Failed to fetch holdings for {broker}: {exc}",
                    ExitCode.AUTH_SESSION_FAILURE,
                    details={"broker": broker},
                ) from exc

        if context.output_format != "json":
            await print_holdings(holdings)

        return ExitCode.SUCCESS, {
            "mock": context.mock_brokers,
            "broker": broker,
            "ticker": args.ticker,
            "holdings": holdings,
        }

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
    validate_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["validate"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS
        and "validate" in BROKER_FUNCTIONS.get(broker_name, {})
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

    # Use order processor for concurrent execution with better error handling
    if context.output_format != "json":
        print(
            f"\n{args.action.upper()} {args.quantity} {args.ticker} @ ${args.price if args.price else 'market'}"
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

    results = await order_processor.process_orders(
        [order],
        trade_functions,
        cli_response_fn,  # Use wrapper that handles force_redraw parameter
        validate_functions=validate_functions,
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
        "messages": cli_messages,
    }


def _add_shared_cli_args(parser, suppress_defaults: bool = False):
    default = argparse.SUPPRESS if suppress_defaults else None
    text_default = argparse.SUPPRESS if suppress_defaults else "text"
    false_default = argparse.SUPPRESS if suppress_defaults else False
    empty_default = argparse.SUPPRESS if suppress_defaults else ""
    default_qty_default = argparse.SUPPRESS if suppress_defaults else 1
    db_default = argparse.SUPPRESS if suppress_defaults else "logs/automation.sqlite3"

    parser.add_argument(
        "--broker",
        action="append",
        default=default,
        help="Broker(s) to use. Can be specified multiple times (e.g., --broker Public --broker Robinhood)",
    )
    parser.add_argument(
        "--output",
        choices=["text", "json"],
        default=text_default,
        help="Output format (reserved for agent-safe machine output)",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        default=false_default,
        help="Disable interactive input prompts (reserved for agent mode)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=false_default,
        help="Validate execution without placing orders (reserved for agent mode)",
    )
    parser.add_argument(
        "--mock-brokers",
        action="store_true",
        default=false_default,
        help="Use deterministic mock broker responses instead of live broker calls",
    )
    parser.add_argument(
        "--log-format",
        choices=["text", "jsonl"],
        default=text_default,
        help="Log format (reserved for structured agent logging)",
    )
    parser.add_argument(
        "--request-id",
        default=empty_default,
        help="Optional request correlation id",
    )
    parser.add_argument(
        "--log-file",
        default=empty_default,
        help="Optional path for structured logs when --log-format jsonl is used",
    )
    parser.add_argument(
        "--from-file",
        default=empty_default,
        help="Load and execute batch orders from a JSON file",
    )
    parser.add_argument(
        "--recap-file",
        default=empty_default,
        help="Path to chat recap text file for automate action",
    )
    parser.add_argument(
        "--db-path",
        default=db_default,
        help="SQLite path for automation state and dedupe",
    )
    parser.add_argument(
        "--default-qty",
        type=int,
        default=default_qty_default,
        help="Default quantity used for generated buy/sell orders",
    )
    parser.add_argument(
        "--today-mmdd",
        default=empty_default,
        help="Override current date in MM/DD for automation evaluation",
    )


def _build_parser():
    parser = RuntimeArgumentParser(
        description="A one click solution to submitting an order across multiple brokers"
    )
    _add_shared_cli_args(parser)
    parser.set_defaults(quantity=None, ticker=None, price=None, force=False)

    shared_parent = argparse.ArgumentParser(add_help=False)
    _add_shared_cli_args(shared_parent, suppress_defaults=True)

    subparsers = parser.add_subparsers(dest="action", metavar="action")

    for action in ("buy", "sell"):
        trade_parser = subparsers.add_parser(
            action,
            parents=[shared_parent],
            help=f"{action.capitalize()} across selected brokers",
        )
        trade_parser.add_argument("quantity", type=int, help="Quantity to trade")
        trade_parser.add_argument("ticker", help="Ticker symbol")
        trade_parser.add_argument(
            "price", nargs="?", type=float, help="Price for limit order (optional)"
        )

    setup_parser = subparsers.add_parser(
        "setup", parents=[shared_parent], help="Configure broker credentials"
    )
    setup_parser.set_defaults(quantity=None, ticker=None, price=None)

    holdings_parser = subparsers.add_parser(
        "holdings", parents=[shared_parent], help="Fetch holdings for one broker"
    )
    holdings_parser.add_argument("ticker", nargs="?", help="Ticker symbol")
    holdings_parser.set_defaults(quantity=None, price=None)

    health_parser = subparsers.add_parser(
        "health", parents=[shared_parent], help="Check broker configuration health"
    )
    health_parser.set_defaults(quantity=None, ticker=None, price=None)

    automate_parser = subparsers.add_parser(
        "automate", parents=[shared_parent], help="Run automation from recap state"
    )
    automate_parser.set_defaults(quantity=None, ticker=None, price=None)

    sweep_parser = subparsers.add_parser(
        "sweep",
        parents=[shared_parent],
        help="Detect post-reverse-split shares across brokers",
    )
    sweep_parser.add_argument("ticker", help="Ticker symbol to sweep")
    sweep_parser.add_argument(
        "--ratio",
        required=True,
        help="Reverse split ratio in N:D format, for example 1:25",
    )
    sweep_parser.add_argument(
        "--pre-qty",
        type=int,
        default=1,
        help="Pre-split shares purchased per broker/account",
    )
    sweep_parser.add_argument(
        "--force",
        action="store_true",
        help="Include ambiguous positions in the sellable result set",
    )
    sweep_parser.set_defaults(quantity=None, price=None)

    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    context = ExecutionContext(
        command=(args.action or ("batch" if args.from_file else None)),
        output_format=args.output,
        non_interactive=args.non_interactive,
        dry_run=args.dry_run,
        mock_brokers=args.mock_brokers,
        log_format=args.log_format,
        log_file=args.log_file,
        request_id=args.request_id,
    )

    try:
        _emit_log_event(
            context,
            "info",
            "command_start",
            {
                "action": args.action,
                "from_file": args.from_file,
                "dry_run": args.dry_run,
                "mock_brokers": args.mock_brokers,
                "non_interactive": args.non_interactive,
            },
        )

        if context.non_interactive:
            setup_tui_input_interception()
            set_non_interactive_mode(True)

        if not any([args.action, args.quantity, args.ticker, args.from_file]):
            if context.non_interactive:
                raise CliRuntimeError(
                    "Action is required in --non-interactive mode",
                    ExitCode.INVALID_ARGS,
                )
            if context.output_format == "json":
                raise CliRuntimeError(
                    "Action is required when using --output json",
                    ExitCode.INVALID_ARGS,
                )
            run_tui()
            return int(ExitCode.SUCCESS)
        else:
            exit_code, data = asyncio.run(run_cli(args, parser, context))
            _emit_runtime_success(context, data)
            _emit_log_event(
                context,
                "info",
                "command_success",
                {"exit_code": int(exit_code)},
            )
            return int(exit_code)
    except CliRuntimeError as err:
        _emit_runtime_error(err, context)
        _emit_log_event(
            context,
            "error",
            "command_error",
            {"exit_code": int(err.exit_code), "message": err.message},
        )
        return int(err.exit_code)
    except Exception as exc:
        error = CliRuntimeError(
            f"Unexpected internal error: {exc}",
            ExitCode.INTERNAL_ERROR,
        )
        _emit_runtime_error(error, context)
        _emit_log_event(
            context,
            "error",
            "command_error",
            {"exit_code": int(error.exit_code), "message": error.message},
        )
        return int(error.exit_code)
    finally:
        if context.non_interactive:
            set_non_interactive_mode(False)
            restore_original_input()
        # Shutdown sessions and close shared HTTP client
        try:
            asyncio.run(session_manager.shutdown())
        except Exception:
            session_manager.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
