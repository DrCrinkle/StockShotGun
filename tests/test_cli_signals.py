"""Tests for the `signals` CLI handler (`cli.signals._run_signals`).

The handler follows main.py's uniform contract: it takes
(args, parser, context), returns (ExitCode, data-dict), and never prints
JSON itself — envelope serialization happens centrally in main().
"""

import argparse
import asyncio
from datetime import datetime

from automation_recap import AutomationRecapStore
from cli.signals import _run_signals
from cli_runtime import ExecutionContext, ExitCode
from signals.nasdaq import CalendarSignal

NOW = datetime(2026, 7, 1, 12, 0, 0)


def _args(tmp_path, action="scan", status=None):
    return argparse.Namespace(
        signals_action=action,
        db_path=str(tmp_path / "automation.sqlite3"),
        status=status,
    )


def _context(output_format="json"):
    return ExecutionContext(command="signals", output_format=output_format)


def _fake_fetcher(signals):
    async def fetch():
        return signals

    return fetch


async def _must_not_fetch():
    raise AssertionError("list must not invoke the fetcher")


def _signal(ticker="ABCD", ratio="1:25", effective_date="2026-07-14"):
    return CalendarSignal(
        ticker=ticker,
        ratio=ratio,
        effective_date=effective_date,
        company=f"{ticker} Corp",
        raw={},
    )


def test_scan_persists_and_reports_counts(tmp_path):
    exit_code, data = asyncio.run(
        _run_signals(
            _args(tmp_path),
            None,
            _context(),
            fetcher=_fake_fetcher([_signal()]),
            now=NOW,
        )
    )
    assert exit_code == ExitCode.SUCCESS
    assert data["counts"] == {"new": 1, "seen": 0, "expired": 0}
    assert data["signals"][0]["ticker"] == "ABCD"

    store = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    assert len(store.list_calendar_signals(status="new")) == 1
    store.close()


def test_scan_expires_stale_signals(tmp_path):
    stale = _signal("OLDX", ratio="1:10", effective_date="2026-06-01")
    exit_code, data = asyncio.run(
        _run_signals(
            _args(tmp_path),
            None,
            _context(),
            fetcher=_fake_fetcher([stale]),
            now=NOW,
        )
    )
    assert exit_code == ExitCode.SUCCESS
    assert data["counts"]["expired"] == 1
    # scan output lists 'new' signals only; the freshly-expired one is gone
    assert data["signals"] == []


def test_list_reads_without_network(tmp_path):
    asyncio.run(
        _run_signals(
            _args(tmp_path),
            None,
            _context(),
            fetcher=_fake_fetcher([_signal()]),
            now=NOW,
        )
    )

    # list must never touch the network / fetcher at all.
    exit_code, data = asyncio.run(
        _run_signals(
            _args(tmp_path, action="list"),
            None,
            _context(),
            fetcher=_must_not_fetch,
            now=NOW,
        )
    )
    assert exit_code == ExitCode.SUCCESS
    assert data["signals"][0]["ticker"] == "ABCD"


def test_list_status_filter(tmp_path):
    asyncio.run(
        _run_signals(
            _args(tmp_path),
            None,
            _context(),
            fetcher=_fake_fetcher(
                [_signal(), _signal("OLDX", effective_date="2026-06-01")]
            ),
            now=NOW,
        )
    )

    _, data = asyncio.run(
        _run_signals(
            _args(tmp_path, action="list", status="expired"),
            None,
            _context(),
            fetcher=_must_not_fetch,
            now=NOW,
        )
    )
    assert [row["ticker"] for row in data["signals"]] == ["OLDX"]

    # default `list` (no --status) returns everything
    _, data_all = asyncio.run(
        _run_signals(
            _args(tmp_path, action="list"),
            None,
            _context(),
            fetcher=_must_not_fetch,
            now=NOW,
        )
    )
    assert len(data_all["signals"]) == 2
