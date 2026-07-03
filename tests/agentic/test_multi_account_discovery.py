"""Multi-account discovery — `session_manager_accounts` reads cached
account_ids from the broker's session dict so per-broker fan-out hits every
real account (Robinhood IRA + taxable, Fennel's multi-account, etc.) rather
than collapsing to a single "primary" leg.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic._base import (
    BrokerMCPServer,
    BrokerMCPSpec,
    make_session_accounts_fn,
    session_manager_accounts,
)


@pytest.fixture(autouse=True)
def restore_session_state():
    """Save/restore the global session_manager.sessions dict so test
    mutations don't leak across cases."""
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    saved = dict(session_manager.sessions)
    yield
    session_manager.sessions.clear()
    session_manager.sessions.update(saved)


def test_session_manager_accounts_returns_primary_when_no_session():
    """Default fallback: no session initialized → ['primary']."""
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    session_manager.sessions.clear()
    accounts = asyncio.run(session_manager_accounts("Fennel"))
    assert accounts == ["primary"]


def test_session_manager_accounts_reads_account_ids_from_session():
    """Real path: session_manager has a session with account_ids field →
    returns those account_ids."""
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    session_key = BrokerConfig.get_session_key("Fennel")
    assert session_key, "Fennel must have a session_key in BrokerConfig"
    session_manager.sessions[session_key] = {
        "token": "fake",
        "account_ids": ["acct-001", "acct-002", "acct-003"],
    }
    accounts = asyncio.run(session_manager_accounts("Fennel"))
    assert accounts == ["acct-001", "acct-002", "acct-003"]


def test_session_manager_accounts_handles_empty_account_ids():
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    session_key = BrokerConfig.get_session_key("Fennel")
    session_manager.sessions[session_key] = {"token": "fake", "account_ids": []}
    accounts = asyncio.run(session_manager_accounts("Fennel"))
    assert accounts == ["primary"]


def test_session_manager_accounts_handles_session_without_account_ids_field():
    """Brokers that don't cache account_ids (yet) fall back gracefully."""
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    session_key = BrokerConfig.get_session_key("Tradier")
    if not session_key:
        pytest.skip("Tradier has no session_key")
    session_manager.sessions[session_key] = {"token": "fake"}
    accounts = asyncio.run(session_manager_accounts("Tradier"))
    assert accounts == ["primary"]


def test_session_manager_accounts_unknown_broker_returns_primary():
    accounts = asyncio.run(session_manager_accounts("NotARealBroker"))
    assert accounts == ["primary"]


def test_make_session_accounts_fn_binds_to_broker_name():
    """The factory returns a closure that consistently reads the same broker
    name across multiple calls."""
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    fennel_key = BrokerConfig.get_session_key("Fennel")
    session_manager.sessions[fennel_key] = {
        "account_ids": ["fennel-1", "fennel-2"]
    }

    fennel_fn = make_session_accounts_fn("Fennel")
    out1 = asyncio.run(fennel_fn())
    assert out1 == ["fennel-1", "fennel-2"]
    # Call again — closure re-reads, not cached
    session_manager.sessions[fennel_key] = {"account_ids": ["fennel-1"]}
    out2 = asyncio.run(fennel_fn())
    assert out2 == ["fennel-1"]


def test_fennel_spec_discovers_single_primary_leg():
    """Final-review C1 pin: Fennel is `multi_account=False` — its trade fn
    fans out internally over ALL session accounts (brokers/fennel.py), so
    per-account legs would double-place (2 accounts = 2 legs x 2 internal
    orders = 4 live orders). Discovery must therefore yield ONE "primary"
    leg even when the session caches real account_ids."""
    from agentic._base import build_broker_mcp_spec  # type: ignore[import-untyped]
    from brokers import registry  # type: ignore[import-untyped]
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    assert registry.get("Fennel").multi_account is False

    spec = build_broker_mcp_spec(registry.get("Fennel"))
    fennel_key = BrokerConfig.get_session_key("Fennel")
    session_manager.sessions[fennel_key] = {
        "account_ids": ["primary-fennel", "ira-fennel"]
    }
    accounts = asyncio.run(spec.list_accounts_fn())
    assert accounts == ["primary"]


def test_multi_account_registry_flag_maps_to_session_driven_discovery():
    """The `multi_account=True` -> session-driven-discovery mapping itself
    still works (no real broker sets it today — see final-review C1); a
    synthetic registry spec proves the mechanism for the future
    account-scoped TradeFn."""
    from agentic._base import build_broker_mcp_spec  # type: ignore[import-untyped]
    from brokers import registry as registry_mod  # type: ignore[import-untyped]
    from brokers.base import BrokerConfig  # type: ignore[import-untyped]
    from brokers.session_manager import session_manager  # type: ignore[import-untyped]

    fennel = registry_mod.get("Fennel")
    synthetic = registry_mod.BrokerSpec(
        name="Fennel",  # reuse Fennel's refs/session key; flag flipped
        session_key=fennel.session_key,
        env_vars=fennel.env_vars,
        trade=fennel.trade,
        holdings=fennel.holdings,
        session_getter=fennel.session_getter,
        multi_account=True,
    )
    spec = build_broker_mcp_spec(synthetic)
    fennel_key = BrokerConfig.get_session_key("Fennel")
    session_manager.sessions[fennel_key] = {
        "account_ids": ["primary-fennel", "ira-fennel"]
    }
    accounts = asyncio.run(spec.list_accounts_fn())
    assert accounts == ["primary-fennel", "ira-fennel"]


def test_broker_server_list_accounts_at_broker_uses_spec_fn():
    """End-to-end: BrokerMCPServer.list_accounts_at_broker calls SPEC.list_accounts_fn."""
    log: list[list[str]] = []

    async def custom_accounts():
        result = ["acc-A", "acc-B", "acc-C"]
        log.append(result)
        return result

    async def fake_trade(side, qty, ticker, price):
        return {"ok": True}

    async def fake_holdings(ticker=None):
        return {}

    spec = BrokerMCPSpec(
        name="Custom",
        trade_fn=fake_trade,
        holdings_fn=fake_holdings,
        list_accounts_fn=custom_accounts,
    )
    from enforcement import AuditLog, EnforcementCore, ProposalStore
    from enforcement.circuit_breaker import CircuitBreaker

    import tempfile

    tmp = Path(tempfile.mkdtemp())
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp / "p.sqlite"),
        audit=AuditLog(tmp / "a.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    server = BrokerMCPServer(spec, core=core)
    accounts = asyncio.run(server.list_accounts_at_broker())
    assert accounts == ["acc-A", "acc-B", "acc-C"]
    assert log == [["acc-A", "acc-B", "acc-C"]]
