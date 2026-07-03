from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec
from agentic.router import (
    BrokerServerAccountStatusProvider,
    NullAccountStatusProvider,
    Router,
    build_router_fastmcp_server,
)
from agentic._base import _default_single_account
from enforcement import (
    AuditLog,
    EnforcementCore,
    LiveOrderRequiresConfirmation,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

EXPECTED_ROUTER_TOOLS = {
    "list_brokers",
    "get_holdings",
    "propose_order",
    "execute_order",
    "place_order",
    "get_rsa_trade",
    "run_sweep",
    "sell_arrived",
    "recap_ingest",
    "scan_signals",
    "dismiss_signal",
    "promote_signal",
}


def _fake_broker(name: str, trade_log: list[Any]) -> BrokerMCPSpec:
    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        trade_log.append((name, side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker: str | None = None) -> Any:
        return {ticker or "ALL": 100.0}

    return BrokerMCPSpec(
        name=name,
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
    )


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


@pytest.fixture
def fake_router(core: EnforcementCore) -> tuple[Router, list[Any]]:
    log: list[Any] = []
    specs = [_fake_broker("FakeA", log), _fake_broker("FakeB", log), _fake_broker("FakeC", log)]
    servers = {s.name: BrokerMCPServer(s, core=core) for s in specs}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    return router, log


def test_router_lists_all_brokers(fake_router):
    router, _ = fake_router
    out = asyncio.run(router.list_brokers())
    assert out["count"] == 3
    names = {b["broker"] for b in out["brokers"]}
    assert names == {"FakeA", "FakeB", "FakeC"}


def test_router_get_holdings_fans_out(fake_router):
    router, _ = fake_router
    out = asyncio.run(router.get_holdings(ticker="TSLA"))
    assert out["ticker"] == "TSLA"
    assert len(out["brokers"]) == 3
    assert all(b["ok"] for b in out["brokers"])


def test_router_get_holdings_subset(fake_router):
    router, _ = fake_router
    out = asyncio.run(router.get_holdings(brokers=["FakeA", "FakeC"]))
    names = [b["broker"] for b in out["brokers"]]
    assert names == ["FakeA", "FakeC"]


def test_router_unknown_broker_rejected(fake_router):
    router, _ = fake_router
    with pytest.raises(Exception):
        asyncio.run(router.get_holdings(brokers=["NotARealBroker"]))


def test_router_propose_then_execute_dry_run(fake_router):
    router, trade_log = fake_router
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=10.0,
            side="buy",
            brokers=["FakeA", "FakeB"],
            price=5.0,
            dry_run=True,
        )
    )
    assert proposal["proposal_id"]
    assert proposal["estimated_usd"] == 10.0 * 5.0 * 2  # 2 brokers
    assert set(proposal["brokers"]) == {"FakeA", "FakeB"}

    result = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=True,
        )
    )
    assert result["success_count"] == 2
    assert result["failure_count"] == 0
    assert all(r["ok"] for r in result["results"])
    # Dry-run must NOT hit broker SDKs
    assert trade_log == []


def test_router_propose_then_execute_live(fake_router):
    router, trade_log = fake_router
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=2.0,
            side="buy",
            brokers=["FakeA", "FakeB"],
            price=5.0,
            dry_run=False,
        )
    )
    result = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )
    )
    assert result["success_count"] == 2
    # Each broker SDK invoked exactly once
    broker_calls = {entry[0] for entry in trade_log}
    assert broker_calls == {"FakeA", "FakeB"}


def test_router_live_place_order_without_token_rejected(fake_router):
    router, _ = fake_router
    with pytest.raises(Exception):
        asyncio.run(
            router.place_order(
                ticker="TSLA",
                qty=2.0,
                side="buy",
                brokers=["FakeA"],
                price=5.0,
                dry_run=False,
                proposal_id=None,
            )
        )


def test_router_place_order_dry_run_auto_proposes(fake_router):
    router, trade_log = fake_router
    out = asyncio.run(
        router.place_order(
            ticker="TSLA",
            qty=2.0,
            side="buy",
            brokers=["FakeA"],
            price=5.0,
            dry_run=True,
        )
    )
    assert out["success_count"] == 1
    assert trade_log == []  # dry-run never reaches SDK


def test_router_unknown_proposal_id_rejected(fake_router):
    router, trade_log = fake_router
    result = asyncio.run(
        router.execute_order(
            proposal_id="not-a-real-proposal-id",
            dry_run=False,
        )
    )
    assert result["rejected"] is True
    assert result["reason"] == "proposal_not_found"
    assert trade_log == []


def test_router_per_leg_tokens_are_single_use(fake_router):
    """Per-leg single-use: second execute_order hits token_already_used per leg."""
    router, _ = fake_router
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=2.0,
            side="buy",
            brokers=["FakeA"],
            price=5.0,
            dry_run=False,
        )
    )
    first = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )
    )
    assert first["success_count"] == 1
    second = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )
    )
    # Per-leg single-use → second call sees all legs already consumed
    assert second["success_count"] == 0
    assert second["failure_count"] == 1
    assert second["results"][0]["reason"] == "token_already_used"


def test_router_partial_failure_isolates_legs(fake_router, core: EnforcementCore):
    """One broker's SDK exploding does not halt sibling legs."""
    router, trade_log = fake_router

    # Replace FakeB's trade fn with one that raises
    async def boom(side, qty, ticker, price):
        trade_log.append(("FakeB-boom",))
        raise RuntimeError("FakeB SDK explosion")

    router.broker_servers["FakeB"].spec = BrokerMCPSpec(  # type: ignore[misc]
        name="FakeB",
        trade_fn=boom,
        holdings_fn=router.broker_servers["FakeB"].spec.holdings_fn,
        list_accounts_fn=router.broker_servers["FakeB"].spec.list_accounts_fn,
    )

    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["FakeA", "FakeB"],
            price=5.0,
            dry_run=False,
        )
    )
    result = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )
    )
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    by_broker = {r["broker"]: r for r in result["results"]}
    assert by_broker["FakeA"]["ok"]
    assert not by_broker["FakeB"]["ok"]
    assert by_broker["FakeB"]["reason"] == "broker_sdk_failure"


def test_router_fastmcp_registers_expected_tools(fake_router):
    router, _ = fake_router
    app = build_router_fastmcp_server(router)
    tools = asyncio.run(app.list_tools())
    names = {t.name for t in tools}
    assert names == EXPECTED_ROUTER_TOOLS


def test_router_loads_all_real_brokers(core: EnforcementCore):
    router = Router.from_all_brokers(core=core)
    out = asyncio.run(router.list_brokers())
    assert out["count"] == 13
    names = {b["broker"] for b in out["brokers"]}
    assert names == {
        "Robinhood", "Tradier", "TastyTrade", "Public", "Firstrade",
        "Fennel", "Schwab", "BBAE", "DSPAC", "SoFi", "Webull",
        "WellsFargo", "Chase",
    }


def _multi_account_spec(
    name: str,
    accounts: list[str],
    log: list[Any],
    *,
    account_scoped_trade: bool = True,
) -> BrokerMCPSpec:
    """Simulates a FUTURE account-scoped multi-account broker (default):
    `account_scoped_trade=True` lets its real-account-id legs place. Pass
    False to simulate today's account-blind trade fns and exercise the
    dispatch guard (final-review C1)."""
    async def fake_trade(side, qty, ticker, price):
        log.append((name, side, qty, ticker, price))
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {ticker or "ALL": 0.0}

    async def fake_list():
        return accounts

    return BrokerMCPSpec(
        name=name,
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
        list_accounts_fn=fake_list,
        account_scoped_trade=account_scoped_trade,
    )


def test_router_fans_out_per_account_for_multi_account_broker(core: EnforcementCore):
    """A broker reporting 2 accounts produces 2 legs per fan-out call."""
    log: list[Any] = []
    spec_multi = _multi_account_spec("RobinhoodFake", ["taxable", "ira"], log)
    spec_single = _fake_broker("FennelFake", log)
    servers = {
        s.name: BrokerMCPServer(s, core=core)
        for s in (spec_multi, spec_single)
    }
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["RobinhoodFake", "FennelFake"],
            price=5.0,
            dry_run=False,
        )
    )
    # 2 RobinhoodFake accounts + 1 FennelFake account = 3 legs
    assert proposal["leg_count"] == 3
    assert proposal["accounts_by_broker"]["RobinhoodFake"] == ["taxable", "ira"]
    assert proposal["accounts_by_broker"]["FennelFake"] == ["primary"]
    # Estimated USD = qty * price * leg_count = 1 * 5 * 3 = 15
    assert proposal["estimated_usd"] == 15.0

    result = asyncio.run(
        router.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )
    )
    assert result["success_count"] == 3
    assert result["failure_count"] == 0
    accounts_hit = {r["account_id"] for r in result["results"]}
    assert accounts_hit == {"taxable", "ira", "primary"}


def test_multi_account_fan_out_with_blind_trade_fn_fails_per_leg(
    core: EnforcementCore,
):
    """Final-review C1 guard, engine-level: a multi-account discovery paired
    with an account-BLIND trade fn (today's only kind) must fail each real-
    account leg loudly — never place N blind orders."""
    log: list[Any] = []
    spec = _multi_account_spec(
        "BlindMulti", ["taxable", "ira"], log, account_scoped_trade=False
    )
    router = Router(
        broker_servers={spec.name: BrokerMCPServer(spec, core=core)},
        core=core,
        provider=NullAccountStatusProvider(),
    )
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["BlindMulti"],
            price=5.0,
            dry_run=False,
        )
    )
    assert proposal["leg_count"] == 2
    result = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    assert result["success_count"] == 0
    assert result["failure_count"] == 2
    assert all(
        r["reason"] == "account_scoped_dispatch_unsupported"
        for r in result["results"]
    )
    assert log == []  # zero live orders placed


def test_discover_accounts_dedupes_order_preserving(core: EnforcementCore):
    """Final-review I3: duplicate account ids in a broker's session account
    list must yield ONE leg each (two legs = two orders on one account)."""
    log: list[Any] = []
    spec = _multi_account_spec(
        "DupeBroker", ["a1", "a1", "a2", "a1", "a2"], log
    )
    router = Router(
        broker_servers={spec.name: BrokerMCPServer(spec, core=core)},
        core=core,
        provider=NullAccountStatusProvider(),
    )
    discovered = asyncio.run(router._discover_accounts(["DupeBroker"]))
    assert discovered == {"DupeBroker": ["a1", "a2"]}

    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["DupeBroker"],
            price=5.0,
            dry_run=False,
        )
    )
    assert proposal["leg_count"] == 2
    assert proposal["accounts_by_broker"] == {"DupeBroker": ["a1", "a2"]}


def test_execute_order_missing_broker_server_is_per_leg_failure(
    fake_router, core: EnforcementCore
):
    """Final-review M1: a proposal leg whose broker server is gone at execute
    time must come back as a per-leg `broker_unavailable` failure, not a raw
    KeyError aborting the whole fan-out."""
    router, trade_log = fake_router
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["FakeA", "FakeB"],
            price=5.0,
            dry_run=False,
        )
    )
    del router.broker_servers["FakeB"]
    result = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    assert result["success_count"] == 1
    assert result["failure_count"] == 1
    by_broker = {r["broker"]: r for r in result["results"]}
    assert by_broker["FakeA"]["ok"]
    missing = by_broker["FakeB"]
    assert missing["ok"] is False
    assert missing["reason"] == "broker_unavailable"
    assert missing["account_id"] == "primary"
    assert missing["detail"]
    # Only FakeA's SDK was touched.
    assert all(entry[0] == "FakeA" for entry in trade_log)


def test_execute_order_exception_leg_keeps_full_leg_shape(
    fake_router, core: EnforcementCore
):
    """Final-review M1: a leg whose dispatch RAISES (rather than returning a
    PlaceResult) must still render as a full leg dict — account_id, reason,
    detail — so renderers and completion tracking treat it like any other
    failed leg."""
    router, _ = fake_router
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["FakeA"],
            price=5.0,
            dry_run=False,
        )
    )

    async def explode(**kwargs):
        raise RuntimeError("transport blew up")

    router.broker_servers["FakeA"].place_at_broker = explode  # type: ignore[method-assign]
    result = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    assert result["failure_count"] == 1
    leg = result["results"][0]
    assert leg["ok"] is False
    assert leg["broker"] == "FakeA"
    assert leg["account_id"] == "primary"
    assert leg["reason"] == "exception"
    assert "transport blew up" in leg["detail"]


def test_broker_server_provider_reads_observed_qty(core: EnforcementCore):
    """Real provider returns broker-reported observed_qty (vs Null defaulting to 0)."""
    log: list[Any] = []

    async def held_trade(side, qty, ticker, price):
        log.append(("trade",))
        return {"ok": True}

    async def held_holdings(ticker=None):
        return {"TSLA": 42.0}

    spec = BrokerMCPSpec(
        name="HoldsTsla",
        trade_fn=held_trade,
        holdings_fn=held_holdings,
    )
    servers = {spec.name: BrokerMCPServer(spec, core=core)}
    provider = BrokerServerAccountStatusProvider(servers)
    asyncio.run(provider.prefetch_for("TSLA", ["HoldsTsla"]))
    assert provider.get_observed_qty("HoldsTsla", "primary", "TSLA") == 42.0
    assert provider.get_observed_qty("HoldsTsla", "primary", "AAPL") == 0.0


def test_default_single_account_helper_returns_primary():
    """Sanity: the default list_accounts_fn returns ['primary']."""
    accts = asyncio.run(_default_single_account())
    assert accts == ["primary"]
