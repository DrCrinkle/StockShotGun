"""Tests for the `status` CLI handler (`cli.status._run_status`).

Follows main.py's uniform contract: (args, parser, context) in,
(ExitCode, data-dict) out. JSON envelope emission happens centrally in
main() — the handler itself never prints JSON.
"""

import argparse
import asyncio
from datetime import datetime

from cli.status import _run_status
from cli_runtime import ExecutionContext, ExitCode
from rsa_store import RsaStore
from signals.nasdaq import CalendarSignal
from automation_recap import AutomationRecapStore
from sweep import SweepStatus

NOW = datetime(2026, 7, 1, 12, 0, 0)


def _run(tmp_path, output_format="json"):
    return asyncio.run(
        _run_status(_args(tmp_path), None, _context(output_format), now=NOW)
    )


def _args(tmp_path):
    return argparse.Namespace(db_path=str(tmp_path / "automation.sqlite3"))


def _context(output_format="json"):
    return ExecutionContext(command="status", output_format=output_format)


def test_seeded_snapshot(tmp_path):
    db_path = str(tmp_path / "automation.sqlite3")

    rsa_store = RsaStore(db_path)
    trade_id = rsa_store.create_trade(
        "ABCD", "1:25", expected_split_date="2026-07-14", now=NOW
    )
    position_id = rsa_store.add_position(trade_id, "Fennel", "acct1", 1, now=NOW)
    rsa_store.record_sweep(
        position_id, SweepStatus.AWAITING_SPLIT, 1.0, 1, NOW.isoformat()
    )
    rsa_store.close()

    recap_store = AutomationRecapStore(db_path)
    recap_store.upsert_calendar_signals(
        [
            CalendarSignal(
                ticker="ABCD",
                ratio="1:25",
                effective_date="2026-07-14",
                company="ABCD Corp",
                raw={},
            )
        ],
        source="nasdaq",
        now=NOW,
    )
    recap_store.close()

    exit_code, data = _run(tmp_path)

    assert exit_code == ExitCode.SUCCESS
    assert data["generated_at"] == NOW.isoformat()

    assert len(data["trades"]) == 1
    trade = data["trades"][0]
    assert trade["id"] == trade_id
    assert trade["ticker"] == "ABCD"
    assert trade["split_ratio"] == "1:25"
    assert trade["expected_split_date"] == "2026-07-14"
    assert "created_at" in trade

    assert len(trade["positions"]) == 1
    position = trade["positions"][0]
    assert position["broker"] == "Fennel"
    assert position["status"] == "awaiting_split"

    assert data["calendar_signals"]["new"] == 1
    assert data["buy_signals"]["pending"] == 0


def test_empty_db(tmp_path):
    exit_code, data = _run(tmp_path)

    assert exit_code == ExitCode.SUCCESS
    assert data["trades"] == []
    assert data["calendar_signals"]["new"] == 0
    assert data["buy_signals"]["pending"] == 0
    assert data["pending_sell_triggers"]["pending"] == 0


def test_never_swept_position(tmp_path):
    db_path = str(tmp_path / "automation.sqlite3")

    rsa_store = RsaStore(db_path)
    trade_id = rsa_store.create_trade(
        "WXYZ", "1:10", expected_split_date="2026-08-01", now=NOW
    )
    rsa_store.add_position(trade_id, "Tradier", "acct2", 2, now=NOW)
    rsa_store.close()

    exit_code, data = _run(tmp_path)

    assert exit_code == ExitCode.SUCCESS
    assert len(data["trades"]) == 1
    positions = data["trades"][0]["positions"]
    assert len(positions) == 1
    assert positions[0]["broker"] == "Tradier"
    assert positions[0]["status"] is None
    assert positions[0]["observed_qty"] is None


def test_text_output_includes_ticker_and_never_swept(tmp_path, capsys):
    db_path = str(tmp_path / "automation.sqlite3")

    rsa_store = RsaStore(db_path)
    trade_id = rsa_store.create_trade(
        "WXYZ", "1:10", expected_split_date="2026-08-01", now=NOW
    )
    rsa_store.add_position(trade_id, "Tradier", "acct2", 2, now=NOW)
    rsa_store.close()

    exit_code, _ = _run(tmp_path, output_format="text")

    assert exit_code == ExitCode.SUCCESS
    captured = capsys.readouterr()
    assert "WXYZ" in captured.out
    assert "never-swept" in captured.out
