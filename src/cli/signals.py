"""`signals` command handler — scan the Nasdaq splits calendar into the
calendar_signals table, or list what's staged.

Follows the shared handler contract: (args, parser, context) in,
(ExitCode, data-dict) out. JSON envelope emission happens in main().
"""

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]
from cli_runtime import (  # type: ignore[import-untyped]
    CliRuntimeError,
    ExitCode,
)
from signals.nasdaq import (  # type: ignore[import-untyped]
    SOURCE_NAME,
    CalendarSignal,
    fetch_splits_calendar,
    parse_splits_payload,
)


async def _default_fetcher() -> list[CalendarSignal]:
    payload = await fetch_splits_calendar()
    return parse_splits_payload(payload)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _print_signals(
    signals_action: str, counts: dict[str, int] | None, rows: list[dict[str, Any]]
) -> None:
    if signals_action == "scan":
        print(
            f"Scan complete: {counts['new']} new, {counts['seen']} seen, "
            f"{counts['expired']} expired"
        )
    if not rows:
        print("No calendar signals.")
        return
    for row in rows:
        print(
            f"  [{row['id']:>4}] {row['ticker']:<6} {row['ratio']:<8} "
            f"effective {row['effective_date'] or 'unknown':<12} {row['status']}"
        )


async def _run_signals(
    args,
    parser,
    context,
    fetcher: Callable[[], Awaitable[list[CalendarSignal]]] | None = None,
    now: datetime | None = None,
) -> tuple[ExitCode, dict[str, Any]]:
    now = now or datetime.now()
    store = AutomationRecapStore(args.db_path)
    try:
        counts: dict[str, int] | None = None
        if args.signals_action == "scan":
            counts = {"new": 0, "seen": 0, "expired": 0}
            fetch = fetcher or _default_fetcher
            try:
                signals = await fetch()
            except Exception as exc:
                raise CliRuntimeError(
                    f"Failed to fetch splits calendar: {exc}",
                    ExitCode.INTERNAL_ERROR,
                    details={"source": SOURCE_NAME},
                ) from exc
            counts.update(
                store.upsert_calendar_signals(signals, source=SOURCE_NAME, now=now)
            )
            counts["expired"] = store.expire_stale_calendar_signals(
                today=now.date(), now=now
            )

        # scan shows what's actionable (new); list defaults to everything.
        status_filter = args.status or (
            "new" if args.signals_action == "scan" else None
        )
        rows = [
            _row_to_dict(row) for row in store.list_calendar_signals(status=status_filter)
        ]

        if context.output_format != "json":
            _print_signals(args.signals_action, counts, rows)

        result: dict[str, Any] = {
            "action": args.signals_action,
            "status_filter": status_filter,
            "signals": rows,
        }
        if counts is not None:
            result["counts"] = counts
        return ExitCode.SUCCESS, result
    finally:
        store.close()
