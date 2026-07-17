"""Fennel module unit tests — account-scoped dispatch (ADR 0006 completion, P1 fix).

Exercises `brokers.fennel.fennelTrade` directly (no live creds, no real
network): the session getter short-circuits because we pre-populate
`session_manager.sessions["fennel"]` + `session_manager._initialized`
(mirrors `get_fennel_session`'s own already-initialized branch), and
`http_client.post` is monkeypatched to a fake recorder instead of hitting
Fennel's API.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from brokers import fennel  # noqa: E402
from brokers.base import BrokerConfig  # noqa: E402
from brokers.session_manager import session_manager  # noqa: E402


@pytest.fixture(autouse=True)
def restore_session_state():
    """Save/restore the global session_manager state so this module's
    fixture-injected Fennel session doesn't leak into other test modules."""
    saved_sessions = dict(session_manager.sessions)
    saved_initialized = set(session_manager._initialized)
    yield
    session_manager.sessions.clear()
    session_manager.sessions.update(saved_sessions)
    session_manager._initialized.clear()
    session_manager._initialized.update(saved_initialized)


def _seed_fennel_session(account_ids: list[str]) -> None:
    """Populate the session_manager as if `get_fennel_session` already ran,
    so `fennelTrade` never touches the network for session init."""
    session_key = BrokerConfig.get_session_key("Fennel")
    assert session_key == "fennel"
    session_manager.sessions[session_key] = {
        "access_token": "fake-token",
        "account_ids": account_ids,
    }
    session_manager._initialized.add(session_key)


class _FakePostResponse:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def test_account_scoped_trade_hits_sdk_once_for_the_given_account(monkeypatch):
    """The P1 fix's core guarantee: calling `fennelTrade(..., account_id=...)`
    places exactly ONE order, for exactly that account — the old internal
    loop over every session account_id must NOT run on this path."""
    _seed_fennel_session(["acct-A", "acct-B", "acct-C"])

    calls: list[dict[str, Any]] = []

    async def fake_post(url: str, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _FakePostResponse(status_code=200)

    monkeypatch.setattr(fennel.http_client, "post", fake_post)

    result = asyncio.run(
        fennel.fennelTrade("buy", 10, "TSLA", 5.0, account_id="acct-B")
    )

    assert result is True
    assert len(calls) == 1  # exactly one SDK call — no internal fan-out
    assert calls[0]["json"]["account_id"] == "acct-B"
    assert calls[0]["json"]["symbol"] == "TSLA"
    assert calls[0]["json"]["shares"] == 10


def test_account_scoped_trade_failure_reports_false_without_touching_other_accounts(
    monkeypatch,
):
    """A failed order for the targeted account must report False and still
    never touch the other session accounts (no silent fallback fan-out)."""
    _seed_fennel_session(["acct-A", "acct-B"])

    calls: list[dict[str, Any]] = []

    async def fake_post(url: str, headers=None, json=None, timeout=None):
        calls.append({"json": json})
        return _FakePostResponse(status_code=400, text="rejected")

    monkeypatch.setattr(fennel.http_client, "post", fake_post)

    result = asyncio.run(
        fennel.fennelTrade("buy", 10, "TSLA", 5.0, account_id="acct-A")
    )

    assert result is False
    assert len(calls) == 1
    assert calls[0]["json"]["account_id"] == "acct-A"


def test_legacy_blind_call_without_account_id_still_fans_out_over_all_accounts(
    monkeypatch,
):
    """Back-compat: `fennelTrade` called WITHOUT `account_id` (no caller does
    this anymore — `place_at_broker` always supplies it now that Fennel is
    `account_scoped_trade=True` — see `brokers/registry.py`) still exercises
    the legacy internal-fan-out loop unchanged."""
    _seed_fennel_session(["acct-A", "acct-B"])

    calls: list[dict[str, Any]] = []

    async def fake_post(url: str, headers=None, json=None, timeout=None):
        calls.append({"json": json})
        return _FakePostResponse(status_code=200)

    monkeypatch.setattr(fennel.http_client, "post", fake_post)

    result = asyncio.run(fennel.fennelTrade("buy", 10, "TSLA", 5.0))

    assert result is True
    assert len(calls) == 2
    hit_accounts = {c["json"]["account_id"] for c in calls}
    assert hit_accounts == {"acct-A", "acct-B"}


def test_fennel_registry_spec_is_account_scoped_and_multi_account():
    """Structural pin: the registry entry driving all of this must carry
    both flags together (`brokers/registry.py`)."""
    from brokers import registry

    spec = registry.get("Fennel")
    assert spec.multi_account is True
    assert spec.account_scoped_trade is True
