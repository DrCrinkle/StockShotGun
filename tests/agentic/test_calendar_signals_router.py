"""Calendar-signal router tools: scan_signals, dismiss_signal, promote_signal.

scan_signals ingests the Nasdaq reverse-split calendar (or an injected
fetcher, for tests) into `calendar_signals`; dismiss_signal / promote_signal
act on staged 'new' rows. Read/ingest + staging only — no order ever gets
placed from these tools directly.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic.router import NullAccountStatusProvider, Router
from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker
from signals.nasdaq import CalendarSignal


@pytest.fixture
def engine(tmp_path: Path) -> Router:
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    return Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        automation_store_path=str(tmp_path / "automation.sqlite3"),
    )


def _seed_signal(db_path: str) -> int:
    store = AutomationRecapStore(db_path)
    store.upsert_calendar_signals(
        [
            CalendarSignal(
                ticker="ABCD",
                ratio="1:25",
                effective_date="2099-01-15",
                company="",
                raw={},
            )
        ],
        source="nasdaq_calendar",
        now=datetime(2026, 7, 1),
    )
    row_id = store.list_calendar_signals()[0]["id"]
    store.conn.close()
    return row_id


def test_scan_signals_refresh_false_reads_store(engine: Router):
    _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.scan_signals(refresh=False))
    assert result["ok"] is True
    assert result["signals"][0]["ticker"] == "ABCD"
    assert result["counts"] == {"new": 0, "seen": 0, "expired": 0}


def test_scan_signals_refresh_true_uses_injected_fetcher(engine: Router):
    async def fake_fetch():
        return [
            CalendarSignal(
                ticker="ZZZZ",
                ratio="1:8",
                effective_date="2099-02-01",
                company="",
                raw={},
            )
        ]

    engine.calendar_fetcher = fake_fetch
    result = asyncio.run(engine.scan_signals(refresh=True))
    assert result["counts"]["new"] == 1
    assert result["signals"][0]["ticker"] == "ZZZZ"


def test_dismiss_signal(engine: Router):
    row_id = _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.dismiss_signal(signal_id=row_id, reason="illiquid"))
    assert result["ok"] is True
    store = AutomationRecapStore(engine.automation_store_path)
    assert store.list_calendar_signals()[0]["status"] == "dismissed"
    store.conn.close()


def test_dismiss_unknown_signal_errors_cleanly(engine: Router):
    result = asyncio.run(engine.dismiss_signal(signal_id=99999, reason="whatever"))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_promote_signal_creates_buy_signal(engine: Router):
    row_id = _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.promote_signal(signal_id=row_id))
    assert result["ok"] is True
    assert isinstance(result["buy_signal_id"], int)


def test_promote_unknown_signal_errors_cleanly(engine: Router):
    result = asyncio.run(engine.promote_signal(signal_id=99999))
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_calendar_signal_tools_registered_as_fastmcp_tools(engine: Router):
    from agentic.router import build_router_fastmcp_server

    app = build_router_fastmcp_server(engine)
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert {"scan_signals", "dismiss_signal", "promote_signal"} <= names
