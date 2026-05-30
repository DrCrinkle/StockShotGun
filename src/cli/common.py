"""Helpers shared by more than one CLI handler module.

Anything used by 2+ of cli.sweep / cli.batch / cli.automate / cli.trade (or by
those plus the inline handlers left in main.run_cli) lives here so the handler
modules never need to import from each other in a way that creates a cycle.
"""

import os
import sys
from typing import NoReturn

from brokers import session_manager, BrokerConfig  # type: ignore[import-untyped]
from brokers.registry import broker_functions_map  # type: ignore[import-untyped]

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_FUNCTIONS = broker_functions_map()
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
)


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
