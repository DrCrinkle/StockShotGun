"""F3 finish — router-side RSA tools (get_rsa_trade + run_sweep).

Tests use a real `RsaStore` against a temp sqlite plus fake broker servers
whose holdings return a deterministic per-broker quantity. The classification
path through `sweep.classify_holding` + `resolve_ambiguous_with_date` is
exercised end-to-end via the router.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.router import NullAccountStatusProvider, Router
from enforcement import (
    AuditLog,
    EnforcementCore,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

# Ensure src/ is on sys.path so RsaStore import (lazy in router) resolves
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
def rsa_db(tmp_path: Path) -> tuple[Path, int, list[int]]:
    """Seed an RsaStore with a TSLA 1:25 trade across 2 brokers, 100 shares each."""
    db_path = tmp_path / "automation.sqlite3"
    store = RsaStore(str(db_path))
    trade_id = store.create_trade(
        ticker="TSLA",
        split_ratio="1:25",
        expected_split_date=(date.today() - timedelta(days=30)).isoformat(),
    )
    pos_a = store.add_position(
        trade_id=trade_id, broker="FakeA", account_id="acc1", pre_split_qty=100
    )
    pos_b = store.add_position(
        trade_id=trade_id, broker="FakeB", account_id="acc1", pre_split_qty=100
    )
    store.close()
    return db_path, trade_id, [pos_a, pos_b]


@pytest.fixture
def router(rsa_db: tuple[Path, int, list[int]], tmp_path: Path) -> Router:
    db_path, _, _ = rsa_db
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    # FakeA: post-split share arrived (4 shares for 100 * 1/25). FakeB: still
    # holding pre-split (100 — AMBIGUOUS without date, SHARE_ARRIVED with).
    specs = [
        _holdings_spec("FakeA", {"TSLA": 4.0}),
        _holdings_spec("FakeB", {"TSLA": 100.0}),
    ]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    return Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )


def test_get_rsa_trade_returns_trade_and_positions(
    router: Router, rsa_db: tuple[Path, int, list[int]]
):
    _, trade_id, _ = rsa_db
    out = asyncio.run(router.get_rsa_trade(trade_id))
    assert out["ok"]
    assert out["ticker"] == "TSLA"
    assert out["split_ratio"] == "1:25"
    assert len(out["positions"]) == 2
    brokers = sorted(p["broker"] for p in out["positions"])
    assert brokers == ["FakeA", "FakeB"]
    for p in out["positions"]:
        assert p["pre_split_qty"] == 100


def test_get_rsa_trade_unknown_id_returns_not_found(router: Router):
    out = asyncio.run(router.get_rsa_trade(99999))
    assert out["ok"] is False
    assert out["reason"] == "trade_not_found"


def test_run_sweep_classifies_share_arrived_for_post_split_qty(
    router: Router, rsa_db: tuple[Path, int, list[int]]
):
    """FakeA holds 4 shares post-1:25-split of 100 → share_arrived."""
    _, trade_id, _ = rsa_db
    out = asyncio.run(router.run_sweep(trade_id))
    assert out["ok"]
    fake_a = next(c for c in out["classifications"] if c["broker"] == "FakeA")
    assert fake_a["expected_post_qty"] == 4
    assert fake_a["observed_qty"] == 4.0
    assert fake_a["resolved_status"] == "share_arrived"


def test_run_sweep_classifies_awaiting_split_for_unchanged_qty(
    router: Router, rsa_db: tuple[Path, int, list[int]]
):
    """FakeB still holds 100 shares (== pre_split_qty, != expected_post_qty=4).
    Per `classify_holding`, observed==pre and pre!=expected means the split has
    not processed yet → AWAITING_SPLIT. resolve_ambiguous_with_date only
    rewrites AMBIGUOUS (where observed==pre==expected), so AWAITING_SPLIT is
    preserved end-to-end.
    """
    _, trade_id, _ = rsa_db
    out = asyncio.run(router.run_sweep(trade_id))
    fake_b = next(c for c in out["classifications"] if c["broker"] == "FakeB")
    assert fake_b["pre_split_qty"] == 100
    assert fake_b["expected_post_qty"] == 4
    assert fake_b["observed_qty"] == 100.0
    assert fake_b["initial_status"] == "awaiting_split"
    assert fake_b["resolved_status"] == "awaiting_split"


def test_run_sweep_resolves_ambiguous_via_processing_window(
    rsa_db: tuple[Path, int, list[int]], tmp_path: Path
):
    """Build a TRUE ambiguous case: pre_split_qty == expected_post_qty (1:1
    split). Observed == pre_split means AMBIGUOUS initially. With the
    expected_split_date 30 days ago + non-zero window elapsed, the resolver
    upgrades to SHARE_ARRIVED.
    """
    db_path = tmp_path / "amb.sqlite3"
    store = RsaStore(str(db_path))
    # 1:1 split → expected_post == pre_split → observed==pre==expected → AMBIGUOUS
    trade_id = store.create_trade(
        ticker="AMB",
        split_ratio="1:1",
        expected_split_date=(date.today() - timedelta(days=365)).isoformat(),
    )
    store.add_position(
        trade_id=trade_id, broker="FakeA", account_id="acc1", pre_split_qty=10
    )
    store.close()

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "p_amb.sqlite"),
        audit=AuditLog(tmp_path / "a_amb.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    spec = _holdings_spec("FakeA", {"AMB": 10.0})
    r = Router(
        broker_servers={spec.name: BrokerMCPServer(spec, core=core)},
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    out = asyncio.run(r.run_sweep(trade_id))
    c = out["classifications"][0]
    assert c["initial_status"] == "ambiguous"
    assert c["resolved_status"] == "share_arrived"


def test_run_sweep_lists_would_sell_legs(
    router: Router, rsa_db: tuple[Path, int, list[int]]
):
    _, trade_id, _ = rsa_db
    out = asyncio.run(router.run_sweep(trade_id))
    would_sell = out["would_sell"]
    # At least FakeA should be in would_sell (post-split qty observed)
    assert any(c["broker"] == "FakeA" for c in would_sell)
    # Every would_sell entry must have resolved_status == share_arrived
    assert all(c["resolved_status"] == "share_arrived" for c in would_sell)


def test_run_sweep_handles_unregistered_broker_gracefully(
    rsa_db: tuple[Path, int, list[int]], tmp_path: Path
):
    """A position in the RSA trade for a broker not registered with the router
    must surface as an error in classifications, not blow up the sweep."""
    db_path, _, _ = rsa_db
    # Add a stranded position for a non-registered broker
    store = RsaStore(str(db_path))
    trade_id = store.create_trade(ticker="GME", split_ratio="1:10")
    store.add_position(
        trade_id=trade_id, broker="GhostBroker", account_id="x", pre_split_qty=50
    )
    store.close()

    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "p2.sqlite"),
        audit=AuditLog(tmp_path / "a2.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    router = Router(
        broker_servers={},  # NO brokers registered
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    out = asyncio.run(router.run_sweep(trade_id))
    assert out["ok"]
    assert out["summary"]["error"] >= 1
    ghost = next(c for c in out["classifications"] if c["broker"] == "GhostBroker")
    assert ghost["error"] == "broker_not_registered"


def test_fastmcp_router_registers_rsa_tools():
    """The router's FastMCP surface must expose `get_rsa_trade` + `run_sweep`."""
    from agentic.router import build_router_fastmcp_server

    db_path = Path("/tmp/_fmcp_rsa_test.sqlite")
    db_path.unlink(missing_ok=True)
    core = EnforcementCore(
        proposal_store=ProposalStore(Path("/tmp/_fmcp_props.sqlite")),
        audit=AuditLog(Path("/tmp/_fmcp_audit.jsonl")),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    r = Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    app = build_router_fastmcp_server(r)
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert "get_rsa_trade" in names
    assert "run_sweep" in names
