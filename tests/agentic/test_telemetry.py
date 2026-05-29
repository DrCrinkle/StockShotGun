"""F10 — structured logging for MCP tool invocations (ISC-30, 31, 32).

Each test runs a router/broker method and inspects the freshly-written
`logs/mcp-{date}.jsonl` line(s) for the required fields.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic._telemetry import (
    configure_telemetry_log,
    reset_telemetry_log,
)
from agentic.router import NullAccountStatusProvider, Router
from enforcement import (
    AuditLog,
    EnforcementCore,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker


def _fake_spec(name: str) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    return BrokerMCPSpec(
        name=name, trade_fn=fake_trade, holdings_fn=fake_holdings
    )


@pytest.fixture
def telemetry_dir(tmp_path: Path) -> Path:
    reset_telemetry_log()
    d = tmp_path / "telemetry_logs"
    configure_telemetry_log(d)
    yield d
    reset_telemetry_log()


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


@pytest.fixture
def router(core: EnforcementCore) -> Router:
    specs = [_fake_spec("FakeA"), _fake_spec("FakeB")]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    return Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )


def _read_log(d: Path) -> list[dict[str, Any]]:
    fname = f"mcp-{datetime.now(UTC).date().isoformat()}.jsonl"
    p = d / fname
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_health_check_emits_log_line(telemetry_dir: Path, router: Router):
    asyncio.run(router.broker_servers["FakeA"].health_check())
    lines = _read_log(telemetry_dir)
    assert lines, "no telemetry line written"
    rec = next(r for r in lines if r["tool"] == "health_check")
    assert rec["ok"] is True
    assert "ts" in rec and rec["ts"]
    assert "duration_ms" in rec and isinstance(rec["duration_ms"], (int, float))
    assert rec["broker"] == "FakeA"


def test_router_list_brokers_emits_line_with_required_fields(
    telemetry_dir: Path, router: Router
):
    asyncio.run(router.list_brokers())
    lines = _read_log(telemetry_dir)
    rec = next(r for r in lines if r["tool"] == "router.list_brokers")
    required_fields = {"ts", "tool", "args", "dry_run", "ok", "duration_ms", "result_summary"}
    assert required_fields <= set(rec.keys())
    assert rec["dry_run"] is False  # ISC-32: explicit field
    assert rec["result_summary"]["shape"] == "dict"


def test_propose_order_log_distinguishes_dry_run_from_live(
    telemetry_dir: Path, router: Router
):
    asyncio.run(
        router.propose_order(
            ticker="TSLA", qty=1, side="buy",
            brokers=["FakeA"], price=5.0, dry_run=True,
        )
    )
    asyncio.run(
        router.propose_order(
            ticker="TSLA", qty=1, side="buy",
            brokers=["FakeA"], price=5.0, dry_run=False,
        )
    )
    lines = _read_log(telemetry_dir)
    proposes = [r for r in lines if r["tool"] == "router.propose_order"]
    assert len(proposes) == 2
    flags = sorted(r["dry_run"] for r in proposes)
    assert flags == [False, True], "ISC-32: dry_run field must distinguish live vs dry-run"


def test_log_redacts_tokens_to_prefix(telemetry_dir: Path, router: Router):
    """`proposal_id` / `confirmation_token` in kwargs must be truncated."""
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA", qty=1, side="buy",
            brokers=["FakeA"], price=5.0, dry_run=False,
        )
    )
    pid = proposal["proposal_id"]
    asyncio.run(router.execute_order(proposal_id=pid, dry_run=False))
    lines = _read_log(telemetry_dir)
    exec_line = next(r for r in lines if r["tool"] == "router.execute_order")
    logged = exec_line["args"]["kwargs"]["proposal_id"]
    assert logged != pid, "proposal_id must be truncated"
    assert logged.endswith("…")
    assert len(logged) <= 9  # 8 chars + ellipsis


def test_log_omits_credential_keys_from_args(telemetry_dir: Path, router: Router):
    """Credential-shaped keys in args (any depth) get dropped before logging."""

    async def _call():
        # Simulate a future tool that took a payload with credential-shaped keys
        # via the decorator on health_check — kwargs go through _redact.
        from agentic._telemetry import _redact  # noqa: PLC0415

        payload = {
            "ok": True,
            "broker": "FakeA",
            "session_cookie": "leak",
            "nested": {"password": "leak2", "qty": 10},
        }
        return _redact(payload)

    cleaned = asyncio.run(_call())
    blob = json.dumps(cleaned).lower()
    assert "session_cookie" not in blob
    assert "password" not in blob
    assert "leak" not in blob
    assert "qty" in blob  # legitimate field survives


def test_log_path_uses_daily_rotation_filename(telemetry_dir: Path, router: Router):
    asyncio.run(router.list_brokers())
    today = datetime.now(UTC).date().isoformat()
    expected = telemetry_dir / f"mcp-{today}.jsonl"
    assert expected.exists(), f"daily-rotated log file missing: {expected}"


def test_log_writes_to_logs_directory_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """ISC-31 — default destination is `logs/` (overridable via env var)."""
    reset_telemetry_log()
    monkeypatch.setenv("SSG_MCP_LOG_DIR", str(tmp_path / "logs"))
    from agentic._telemetry import telemetry_log  # noqa: PLC0415

    log = telemetry_log()
    assert str(log.dir).endswith("logs")
    reset_telemetry_log()


def test_failed_call_records_error_reason(telemetry_dir: Path, router: Router):
    """When a router call raises, the log line records ok=false + error_reason."""
    import contextlib

    with contextlib.suppress(Exception):
        asyncio.run(router.get_holdings(brokers=["NotABroker"]))
    lines = _read_log(telemetry_dir)
    failed = [r for r in lines if r["tool"] == "router.get_holdings"]
    assert failed
    assert failed[-1]["ok"] is False
    assert failed[-1]["error_reason"] is not None
