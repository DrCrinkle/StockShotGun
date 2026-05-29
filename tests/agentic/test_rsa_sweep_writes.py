"""F3 v0.2 + v0.3 — sweep state writes + sell-on-arrived tool.

Tests `run_sweep(dry_run=False)` persists classifications via
`rsa_store.record_sweep`, and the new `sell_arrived(trade_id, price=?)` tool
mints a multi-leg sell proposal for every ARRIVED leg.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.router import (
    NullAccountStatusProvider,
    Router,
    build_router_fastmcp_server,
)
from enforcement import (
    AuditLog,
    EnforcementCore,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rsa_store import RsaStore  # type: ignore[import-untyped]


def _holdings_spec(name: str, qty_map: dict[str, float]) -> BrokerMCPSpec:
    async def fake_trade(side, qty, ticker, price):
        return {"ok": True}

    async def fake_holdings(ticker=None):
        if ticker is None:
            return qty_map
        return {ticker: qty_map.get(ticker, 0.0)}

    return BrokerMCPSpec(name=name, trade_fn=fake_trade, holdings_fn=fake_holdings)


@pytest.fixture
def rsa_db_and_router(tmp_path: Path) -> tuple[Path, int, Router]:
    db_path = tmp_path / "automation.sqlite3"
    store = RsaStore(str(db_path))
    trade_id = store.create_trade(
        ticker="TSLA",
        split_ratio="1:25",
        expected_split_date=(date.today() - timedelta(days=30)).isoformat(),
    )
    store.add_position(
        trade_id=trade_id, broker="FakeA", account_id="acc1", pre_split_qty=100
    )
    store.add_position(
        trade_id=trade_id, broker="FakeB", account_id="acc1", pre_split_qty=100
    )
    store.close()

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    # Both brokers report 4 shares post-split (1:25 of 100) → both ARRIVED
    specs = [
        _holdings_spec("FakeA", {"TSLA": 4.0}),
        _holdings_spec("FakeB", {"TSLA": 4.0}),
    ]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    return db_path, trade_id, router


def test_run_sweep_dry_run_does_not_write_sweep_state(
    rsa_db_and_router: tuple[Path, int, Router]
):
    db_path, trade_id, router = rsa_db_and_router
    asyncio.run(router.run_sweep(trade_id, dry_run=True))
    store = RsaStore(str(db_path))
    positions = store.list_positions(trade_id)
    store.close()
    # No sweep_state rows should have been created — positions appear in
    # list_positions but with NULL status/observed_qty (left-joined).
    for p in positions:
        assert p["status"] is None or p["last_checked"] is None


def test_run_sweep_live_writes_sweep_state(
    rsa_db_and_router: tuple[Path, int, Router]
):
    """F3 v0.2 — dry_run=False persists each classification to sweep_state."""
    db_path, trade_id, router = rsa_db_and_router
    out = asyncio.run(router.run_sweep(trade_id, dry_run=False))
    assert out["ok"]
    assert len(out["persisted_position_ids"]) == 2

    store = RsaStore(str(db_path))
    positions = store.list_positions(trade_id)
    store.close()
    statuses = {p["broker"]: p["status"] for p in positions}
    assert statuses["FakeA"] == "share_arrived"
    assert statuses["FakeB"] == "share_arrived"


def test_sell_arrived_proposes_when_legs_ready(
    rsa_db_and_router: tuple[Path, int, Router]
):
    """F3 v0.3 — sell_arrived mints a fan-out sell proposal when ARRIVED legs exist.
    Both legs have qty=4 (post-1:25-split of 100) so they group into ONE proposal."""
    db_path, trade_id, router = rsa_db_and_router
    out = asyncio.run(router.sell_arrived(trade_id, price=10.0))
    assert out["ok"]
    assert out["side"] == "sell"
    assert out["proposal_count"] == 1
    assert out["total_legs"] == 2
    assert out["total_estimated_usd"] == 4.0 * 10.0 * 2
    proposal = out["proposals"][0]
    assert proposal["ok"]
    assert proposal["qty"] == 4.0
    assert proposal["leg_count"] == 2
    assert proposal["proposal_id"]
    arrived_brokers = {p["broker"] for p in out["arrived_positions"]}
    assert arrived_brokers == {"FakeA", "FakeB"}


def test_sell_arrived_returns_no_arrived_positions_when_nothing_ready(
    tmp_path: Path
):
    db_path = tmp_path / "nothing.sqlite3"
    store = RsaStore(str(db_path))
    trade_id = store.create_trade(ticker="WAIT", split_ratio="1:25")
    store.add_position(
        trade_id=trade_id, broker="FakeA", account_id="acc1", pre_split_qty=100
    )
    store.close()

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "p.sqlite"),
        audit=AuditLog(tmp_path / "a.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    # FakeA still holds pre-split qty → AWAITING_SPLIT, not arrived
    spec = _holdings_spec("FakeA", {"WAIT": 100.0})
    router = Router(
        broker_servers={spec.name: BrokerMCPServer(spec, core=core)},
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    out = asyncio.run(router.sell_arrived(trade_id))
    assert out["ok"] is False
    assert out["reason"] == "no_arrived_positions"


def test_sell_arrived_splits_heterogeneous_qty_into_multiple_proposals(
    tmp_path: Path,
):
    """v0.4 — different observed quantities across arrived legs now produce
    one proposal per qty group, not a wholesale rejection."""
    db_path = tmp_path / "het.sqlite3"
    store = RsaStore(str(db_path))
    trade_id = store.create_trade(
        ticker="HET",
        split_ratio="1:25",
        expected_split_date=(date.today() - timedelta(days=30)).isoformat(),
    )
    store.add_position(
        trade_id=trade_id, broker="FakeA", account_id="acc1", pre_split_qty=100
    )
    store.add_position(
        trade_id=trade_id, broker="FakeB", account_id="acc1", pre_split_qty=50
    )
    store.close()

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "p.sqlite"),
        audit=AuditLog(tmp_path / "a.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    # FakeA: 4 shares (100/25), FakeB: 2 shares (50/25) — both arrived, diff qtys
    specs = [
        _holdings_spec("FakeA", {"HET": 4.0}),
        _holdings_spec("FakeB", {"HET": 2.0}),
    ]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    out = asyncio.run(router.sell_arrived(trade_id, price=10.0))
    assert out["ok"]
    assert out["proposal_count"] == 2  # one per qty group
    assert out["total_legs"] == 2  # one leg per proposal
    # Each proposal binds to a distinct qty
    qtys = sorted(p["qty"] for p in out["proposals"] if p["ok"])
    assert qtys == [2.0, 4.0]
    # Each proposal targets exactly one broker (the one matching that qty)
    fake_a_proposal = next(
        p for p in out["proposals"] if p["legs"][0]["broker"] == "FakeA"
    )
    fake_b_proposal = next(
        p for p in out["proposals"] if p["legs"][0]["broker"] == "FakeB"
    )
    assert fake_a_proposal["qty"] == 4.0
    assert fake_b_proposal["qty"] == 2.0


def test_sell_arrived_proposal_executes_end_to_end(
    rsa_db_and_router: tuple[Path, int, Router]
):
    """sell_arrived → execute_order full round-trip with broker SDK invoked."""
    _, trade_id, router = rsa_db_and_router
    out = asyncio.run(router.sell_arrived(trade_id, price=10.0))
    assert out["ok"]
    proposal = out["proposals"][0]
    assert proposal["ok"]
    result = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    assert result["success_count"] == 2
    assert result["failure_count"] == 0


def test_fastmcp_router_registers_sell_arrived():
    """FastMCP router surface must expose `sell_arrived`."""
    core = EnforcementCore(
        proposal_store=ProposalStore(Path("/tmp/_sa_props.sqlite")),
        audit=AuditLog(Path("/tmp/_sa_audit.jsonl")),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    r = Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path="/tmp/_sa_rsa.sqlite",
    )
    app = build_router_fastmcp_server(r)
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert "sell_arrived" in names
