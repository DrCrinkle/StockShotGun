"""`sweep` command handler — post-reverse-split share detection across brokers."""

from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
)
from sweep import (  # type: ignore[import-untyped]
    BROKER_PROFILES,
    UNKNOWN_PROFILE,
    SweepResult,
    SweepStatus,
    parse_ratio,
    resolve_ambiguous_with_date,
    status_summary,
    sweep_all_brokers,
)
from rsa_store import RsaStore  # type: ignore[import-untyped]
from sweep_persistence import (  # type: ignore[import-untyped]
    load_trade_for_sweep,
    persist_sweep_results,
)

from cli.common import _raise_parser_error


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
    rsa_store: RsaStore | None = None
    trade_payload: dict[str, Any] | None = None
    position_lookup: dict[tuple[str, str], int] | None = None

    if args.from_trade is not None:
        if args.ticker is not None or args.ratio is not None:
            _raise_parser_error(
                parser,
                "--from-trade cannot be combined with positional ticker or --ratio",
                context,
            )
        rsa_store = RsaStore(args.db_path)
    elif args.ticker is None or args.ratio is None:
        _raise_parser_error(
            parser,
            "sweep requires either --from-trade or both ticker and --ratio",
            context,
        )

    try:
        if args.from_trade is not None and rsa_store is not None:
            try:
                loaded = load_trade_for_sweep(rsa_store, args.from_trade)
            except (LookupError, ValueError) as exc:
                raise CliRuntimeError(str(exc), ExitCode.INVALID_ARGS) from exc
            trade_payload = loaded
            args.ticker = loaded["ticker"]
            args.ratio = loaded["split_ratio"]
            args.pre_qty = loaded["pre_split_qty"]
            position_lookup = {
                (p["broker"], p["account_id"]): p["position_id"]
                for p in loaded["positions"]
            }
            if not args.broker:
                args.broker = sorted({p["broker"] for p in loaded["positions"]})

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

        if trade_payload is not None and trade_payload["expected_split_date"]:
            today = date.today()
            for result in results:
                profile = BROKER_PROFILES.get(result.broker, UNKNOWN_PROFILE)
                result.status = resolve_ambiguous_with_date(
                    result.status,
                    trade_payload["expected_split_date"],
                    profile.processing_window_days,
                    today,
                )

        if rsa_store is not None and position_lookup is not None:
            observed_at = datetime.now().isoformat()
            persist_sweep_results(rsa_store, position_lookup, results, observed_at)

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
            "trade_id": args.from_trade,
            "selected_brokers": selected_brokers,
            "results": [_sweep_result_to_dict(result) for result in results],
            "summary": summary,
            "sellable": [_sweep_result_to_dict(result) for result in sellable_results],
        }
    finally:
        if rsa_store is not None:
            rsa_store.close()
