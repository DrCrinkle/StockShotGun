import argparse
import asyncio
import contextlib
import io
import json
import os
import sys
import warnings
from typing import Any
from setup import setup  # type: ignore[import-untyped]
from tui import run_tui  # type: ignore[import-untyped]
from tui.input_handler import (  # type: ignore[import-untyped]
    restore_original_input,
    set_non_interactive_mode,
    setup_tui_input_interception,
)
from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()

from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExecutionContext,
    ExitCode,
    build_response_envelope,
)

from cli.common import _credentials_present_for_broker, _raise_parser_error
from cli.automate import _run_automate_from_recap
from cli.signals import _run_signals
from cli.sweep import _run_sweep
from cli.batch import _run_batch_from_file
from cli.trade import run_trade

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
    known_actions = {
        "buy",
        "sell",
        "setup",
        "holdings",
        "health",
        "automate",
        "sweep",
        "signals",
        "status",
    }
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


async def run_cli(args, parser, context) -> tuple[ExitCode, dict[str, Any]]:
    if args.action == "automate":
        return await _run_automate_from_recap(args, parser, context)

    if args.action == "sweep":
        return await _run_sweep(args, parser, context)

    if args.action == "signals":
        return await _run_signals(args, parser, context)

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

    return await run_trade(args, parser, context)


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
    sweep_parser.add_argument(
        "ticker",
        nargs="?",
        default=None,
        help="Ticker symbol to sweep (required unless --from-trade is used)",
    )
    sweep_parser.add_argument(
        "--ratio",
        default=None,
        help="Reverse split ratio in N:D format, for example 1:25 (required unless --from-trade)",
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
    sweep_parser.add_argument(
        "--from-trade",
        type=int,
        default=None,
        help="Load ticker, ratio, expected_split_date, and per-broker positions from rsa_trades by id",
    )
    sweep_parser.set_defaults(quantity=None, price=None)

    signals_parser = subparsers.add_parser(
        "signals",
        parents=[shared_parent],
        help="Scan/list reverse-split signals from the Nasdaq calendar",
    )
    signals_parser.add_argument(
        "signals_action",
        choices=["scan", "list"],
        help="scan: poll the calendar and persist; list: read staged signals",
    )
    signals_parser.add_argument(
        "--status",
        default=None,
        choices=["new", "promoted", "dismissed", "expired"],
        help="Filter listed signals by status (new, promoted, dismissed, expired)",
    )
    signals_parser.set_defaults(quantity=None, ticker=None, price=None)

    return parser


async def _run_cli_and_shutdown(args, parser, context):
    """Run the CLI command and shut sessions down inside the SAME event loop.

    The shared httpx client (``brokers.base.http_client``) binds its
    connection pool to the event loop that first uses it. Closing it from a
    second ``asyncio.run()`` (a different loop) is fragile and can raise
    "Event loop is closed", leaking connections. Running the command and the
    shutdown in one loop keeps the client's lifecycle on a single loop.
    """
    try:
        return await run_cli(args, parser, context)
    finally:
        await session_manager.shutdown()


def _shutdown_sessions_best_effort():
    """Close sessions for paths that don't own a long-lived loop (TUI)."""
    try:
        asyncio.run(session_manager.shutdown())
    except Exception:
        session_manager.cleanup()


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
            try:
                run_tui()
            finally:
                _shutdown_sessions_best_effort()
            return int(ExitCode.SUCCESS)
        else:
            # In JSON mode, stdout must carry only the JSON envelope emitted
            # below. Broker progress messages and third-party broker SDKs
            # print to stdout, so route all of that to stderr while the
            # command runs; humans still see it, machines get clean JSON.
            stdout_guard = (
                contextlib.redirect_stdout(sys.stderr)
                if context.output_format == "json"
                else contextlib.nullcontext()
            )
            with stdout_guard:
                exit_code, data = asyncio.run(
                    _run_cli_and_shutdown(args, parser, context)
                )
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
        # The async shutdown (closing the shared HTTP client) runs inside the
        # command's own event loop via _run_cli_and_shutdown / the TUI path.
        # This is just a loop-free, idempotent safety net for early-exit paths.
        session_manager.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
