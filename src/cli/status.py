"""`status` command handler — read-only aggregate snapshot of RSA state for
the Pulse dashboard to poll.

Follows the shared handler contract: (args, parser, context) in,
(ExitCode, data-dict) out. JSON envelope emission happens in main().
"""

import sqlite3
from datetime import datetime
from typing import Any

from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]
from cli_runtime import ExitCode  # type: ignore[import-untyped]
from rsa_store import RsaStore  # type: ignore[import-untyped]


def _count_by(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
    ).fetchall()
    return {row["k"]: row["n"] for row in rows}


def _position_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "broker": row["broker"],
        "account_id": row["account_id"],
        "pre_split_qty": row["pre_split_qty"],
        "status": row["status"],
        "observed_qty": row["observed_qty"],
        "sold_at": row["sold_at"],
    }


def _trade_to_dict(row: sqlite3.Row, positions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "ticker": row["ticker"],
        "split_ratio": row["split_ratio"],
        "expected_split_date": row["expected_split_date"],
        "created_at": row["created_at"],
        "positions": positions,
    }


def _print_status(data: dict[str, Any]) -> None:
    trades = data["trades"]
    if not trades:
        print("No RSA trades on record.")
    for trade in trades:
        print(
            f"  [{trade['id']:>4}] {trade['ticker']:<6} {trade['split_ratio']:<8} "
            f"expected {trade['expected_split_date'] or 'unknown'}"
        )
        for position in trade["positions"]:
            status = position["status"] or "never-swept"
            print(
                f"        {position['broker']:<12} {position['account_id']:<10} "
                f"pre={position['pre_split_qty']:<4} status={status}"
            )

    for label, counts in (
        ("calendar_signals", data["calendar_signals"]),
        ("buy_signals", data["buy_signals"]),
        ("pending_sell_triggers", data["pending_sell_triggers"]),
    ):
        summary = ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "none"
        print(f"  {label}: {summary}")


async def _run_status(args, parser, context) -> tuple[ExitCode, dict[str, Any]]:
    rsa_store = RsaStore(args.db_path)
    recap_store = AutomationRecapStore(args.db_path)
    try:
        trade_rows = rsa_store.conn.execute(
            "SELECT id, ticker, split_ratio, expected_split_date, created_at "
            "FROM rsa_trades ORDER BY id DESC"
        ).fetchall()

        trades = [
            _trade_to_dict(
                trade_row,
                [_position_to_dict(p) for p in rsa_store.list_positions(trade_row["id"])],
            )
            for trade_row in trade_rows
        ]

        calendar_signals = _count_by(recap_store.conn, "calendar_signals", "status")
        calendar_signals.setdefault("new", 0)

        buy_signals = _count_by(recap_store.conn, "buy_signals", "status")
        buy_signals.setdefault("pending", 0)

        pending_sell_triggers = _count_by(
            recap_store.conn, "pending_sell_triggers", "status"
        )
        pending_sell_triggers.setdefault("pending", 0)

        result: dict[str, Any] = {
            "generated_at": datetime.now().isoformat(),
            "trades": trades,
            "calendar_signals": calendar_signals,
            "buy_signals": buy_signals,
            "pending_sell_triggers": pending_sell_triggers,
        }

        if context.output_format != "json":
            _print_status(result)

        return ExitCode.SUCCESS, result
    finally:
        rsa_store.close()
        recap_store.close()
