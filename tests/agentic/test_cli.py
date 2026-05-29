"""Gated CLI tests — verify every order path routes through enforcement.

Tests use an injected Router built on fake broker SPECs so we exercise the
full CLI → Router → BrokerMCPServer → gate_order chain without spawning
real broker SDKs. A static-check test scans the CLI module for any direct
broker-SDK invocation (which would bypass the gate); if any appears, it fails.
"""

from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.cli import run as cli_run
from agentic.router import NullAccountStatusProvider, Router
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker


def _fake_spec(name: str, trade_log: list[Any]) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        trade_log.append((name, side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    return BrokerMCPSpec(
        name=name, trade_fn=fake_trade, holdings_fn=fake_holdings
    )


@pytest.fixture
def router(tmp_path: Path) -> tuple[Router, list[Any]]:
    log: list[Any] = []
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    specs = [_fake_spec("FakeA", log), _fake_spec("FakeB", log)]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    return Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    ), log


def _capture(argv: list[str], r: Router) -> tuple[int, str]:
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli_run(argv, router=r)
    return rc, buf.getvalue()


def test_cli_list_brokers_returns_both(router):
    r, _ = router
    rc, out = _capture(["--json", "list-brokers"], r)
    assert rc == 0
    payload = json.loads(out)
    assert payload["count"] == 2


def test_cli_dry_run_does_not_hit_broker_sdks(router):
    r, log = router
    rc, out = _capture(
        ["--json", "dry-run", "buy", "10", "TSLA", "--price", "5.0"], r
    )
    assert rc == 0
    payload = json.loads(out)
    assert payload["proposal"]["proposal_id"]
    assert payload["execution"]["success_count"] == 2
    assert log == []  # no broker SDK invoked


def test_cli_propose_then_execute_live_routes_through_gate(router):
    r, log = router
    rc1, out1 = _capture(
        [
            "--json", "propose", "buy", "2", "TSLA",
            "--brokers", "FakeA,FakeB", "--price", "5.0", "--live",
        ],
        r,
    )
    assert rc1 == 0
    proposal = json.loads(out1)
    pid = proposal["proposal_id"]

    rc2, out2 = _capture(["--json", "execute", pid, "--live"], r)
    assert rc2 == 0
    result = json.loads(out2)
    assert result["success_count"] == 2
    assert {entry[0] for entry in log} == {"FakeA", "FakeB"}


def test_cli_execute_unknown_proposal_id_fails(router):
    r, _ = router
    rc, out = _capture(["--json", "execute", "not-a-real-id", "--live"], r)
    payload = json.loads(out)
    assert payload["rejected"] is True
    assert payload["reason"] == "proposal_not_found"
    assert rc == 1  # ERR


def test_cli_audit_verify_clean_log_returns_ok(router):
    r, _ = router
    # Generate some audit entries
    _capture(["--json", "dry-run", "buy", "1", "TSLA", "--price", "5.0"], r)
    rc, out = _capture(["--json", "audit-verify"], r)
    assert rc == 0
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["first_break"] is None


def test_cli_audit_verify_detects_tamper(router):
    r, _ = router
    _capture(["--json", "dry-run", "buy", "1", "TSLA", "--price", "5.0"], r)
    raw = r.core.audit.path.read_text(encoding="utf-8")
    r.core.audit.path.write_text(
        raw.replace('"kind":"propose"', '"kind":"PROPOSE"', 1), encoding="utf-8"
    )
    rc, out = _capture(["--json", "audit-verify"], r)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert rc == 1


def test_cli_holdings_fans_out(router):
    r, _ = router
    rc, out = _capture(["--json", "holdings", "TSLA"], r)
    assert rc == 0
    payload = json.loads(out)
    assert payload["ticker"] == "TSLA"
    assert len(payload["brokers"]) == 2


def test_static_no_direct_broker_sdk_calls_in_cli():
    """Static check: the gated CLI module must NOT call broker `Trade` /
    `GetHoldings` functions directly. Every order path must go through the
    Router (which goes through the gate). If this test fails, someone added
    a direct broker call that bypasses enforcement.
    """
    cli_path = Path(__file__).resolve().parents[2] / "src" / "agentic" / "cli.py"
    source = cli_path.read_text(encoding="utf-8")
    # Strip docstring + comments so example phrasing doesn't trigger
    source_lines = [
        ln for ln in source.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code = "\n".join(source_lines)
    forbidden_patterns = [
        r"\bfrom brokers import\b",
        r"\b\w+Trade\s*\(",
        r"\b\w+GetHoldings\s*\(",
        r"order_processor\.",
    ]
    hits: list[tuple[str, str]] = []
    for pat in forbidden_patterns:
        for m in re.finditer(pat, code):
            line = code[: m.start()].count("\n") + 1
            hits.append((pat, f"line {line}: {m.group(0)}"))
    assert hits == [], f"gated CLI must not call broker SDK directly: {hits}"


def test_static_cli_uses_router():
    """Positive static check: the CLI imports the Router (proving the gated
    path is wired). Belt-and-suspenders with the negative check above.
    """
    cli_path = Path(__file__).resolve().parents[2] / "src" / "agentic" / "cli.py"
    source = cli_path.read_text(encoding="utf-8")
    assert "from agentic.router import Router" in source
