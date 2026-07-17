"""record_rsa_trade — closes the agent trade-capture gap (spec Task 1 of
specs/rsa-agent-operator-plan.md).

`run_sweep` and `sell_arrived` key off `rsa_trades`/`rsa_positions` rows.
Nothing in this repo's production code creates those rows today (confirmed:
`cli/trade.py`, `cli/batch.py`, `cli/automate.py` never call
`RsaStore.create_trade`/`add_position` — only tests exercise the store
directly; the legacy `sweep --from-trade` CLI only *reads* an existing trade
via `load_trade_for_sweep`). `record_rsa_trade` is therefore the first
production writer of these tables — for buys made through
`propose_order`/`execute_order` (agent or otherwise) — using the exact row
shape `RsaStore.create_trade`/`add_position` define and
`load_trade_for_sweep`/`run_sweep` already consume.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agentic.router import NullAccountStatusProvider, Router
from enforcement import AuditLog, EnforcementCore, ProposalStore
from enforcement.circuit_breaker import CircuitBreaker
from rsa_store import RsaStore  # type: ignore[import-untyped]
from sweep import SweepStatus  # type: ignore[import-untyped]


@pytest.fixture
def router_and_db(tmp_path: Path) -> tuple[Router, Path]:
    db_path = tmp_path / "automation.sqlite3"
    core = EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )
    router = Router(
        broker_servers={},
        core=core,
        provider=NullAccountStatusProvider(),
        rsa_store_path=str(db_path),
    )
    return router, db_path


def _leg(broker: str, account_id: str, ok: bool = True) -> dict[str, Any]:
    return {
        "broker": broker,
        "account_id": account_id,
        "ok": ok,
        "dry_run": False,
        "idempotency_key": f"{broker}-{account_id}-key",
        "reason": None if ok else "insufficient_shares",
        "detail": "" if ok else "not enough shares available",
    }


def _execution(
    results: list[dict[str, Any]],
    *,
    qty: float = 1,
    dry_run: bool = False,
    ticker: str = "AREB",
) -> dict[str, Any]:
    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "ticker": ticker,
        "side": "buy",
        "qty": qty,
        "dry_run": dry_run,
        "results": results,
        "success_count": ok_count,
        "failure_count": len(results) - ok_count,
    }


def test_happy_path_creates_trade_and_positions(router_and_db):
    router, db_path = router_and_db
    execution = _execution(
        [
            _leg("Fennel", "acct1"),
            _leg("Fennel", "acct2"),
            _leg("Public", "primary"),
        ],
        qty=1,
    )

    out = _run(router.record_rsa_trade(
        ticker="AREB",
        split_ratio="1:25",
        expected_split_date="2026-05-15",
        execution=execution,
    ))

    assert out == {"ok": True, "trade_id": out["trade_id"], "position_count": 3}

    store = RsaStore(str(db_path))
    trade_row = store.get_trade(out["trade_id"])
    positions = store.get_raw_positions(out["trade_id"])
    store.close()

    assert trade_row["ticker"] == "AREB"
    assert trade_row["split_ratio"] == "1:25"
    assert trade_row["expected_split_date"] == "2026-05-15"
    assert trade_row["signal_id"] is None
    assert len(positions) == 3
    keyed = {(p["broker"], p["account_id"]): p["pre_split_qty"] for p in positions}
    assert keyed == {
        ("Fennel", "acct1"): 1,
        ("Fennel", "acct2"): 1,
        ("Public", "primary"): 1,
    }


def test_dry_run_execution_is_refused_and_writes_nothing(router_and_db):
    router, db_path = router_and_db
    execution = _execution([_leg("Fennel", "acct1")], dry_run=True)

    out = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))

    assert out["ok"] is False
    assert "rehearsal" in out["error"] or "dry" in out["error"].lower()

    store = RsaStore(str(db_path))
    assert store.list_trades() == []
    store.close()


def test_zero_ok_legs_is_refused_and_writes_nothing(router_and_db):
    router, db_path = router_and_db
    execution = _execution(
        [
            _leg("Fennel", "acct1", ok=False),
            _leg("Public", "primary", ok=False),
        ]
    )

    out = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))

    assert out["ok"] is False
    assert set(out.keys()) == {"ok", "error"}
    assert "zero" in out["error"] or "nothing" in out["error"].lower()

    store = RsaStore(str(db_path))
    assert store.list_trades() == []
    store.close()


def test_failed_legs_excluded_from_positions(router_and_db):
    router, db_path = router_and_db
    execution = _execution(
        [
            _leg("Fennel", "acct1", ok=True),
            _leg("Public", "primary", ok=True),
            _leg("Tradier", "primary", ok=False),
        ]
    )

    out = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))

    assert out["ok"] is True
    assert out["position_count"] == 2

    store = RsaStore(str(db_path))
    positions = store.get_raw_positions(out["trade_id"])
    store.close()
    brokers = {p["broker"] for p in positions}
    assert brokers == {"Fennel", "Public"}


def test_signal_id_optional_and_recorded_when_given(router_and_db):
    router, db_path = router_and_db
    execution = _execution([_leg("Fennel", "acct1")])

    out = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution, signal_id=42
    ))

    assert out["ok"] is True
    store = RsaStore(str(db_path))
    trade_row = store.get_trade(out["trade_id"])
    store.close()
    assert trade_row["signal_id"] == 42


def test_duplicate_call_with_same_execution_is_rejected(router_and_db):
    """Guard: a second call carrying an identical execution (same ticker,
    ratio, and (broker, account_id, qty) leg set) against an OPEN trade
    (nothing sold yet) must not silently mint a second trade_id."""
    router, db_path = router_and_db
    execution = _execution(
        [_leg("Fennel", "acct1"), _leg("Public", "primary")]
    )

    first = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))
    assert first["ok"] is True

    second = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))
    assert second["ok"] is False
    assert "duplicate" in second["error"].lower()
    assert second.get("trade_id") == first["trade_id"]

    store = RsaStore(str(db_path))
    trades = store.list_trades()
    store.close()
    assert len(trades) == 1


def test_second_buy_after_first_trade_fully_sold_is_not_a_duplicate(
    router_and_db,
):
    """A legitimate second play of the same ticker/ratio, made after the
    first trade's positions were already sold, is a fresh buy — not a
    re-submission of the old one — and must be allowed."""
    router, db_path = router_and_db
    execution = _execution([_leg("Fennel", "acct1")])

    first = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))
    assert first["ok"] is True

    store = RsaStore(str(db_path))
    positions = store.get_raw_positions(first["trade_id"])
    # A position needs a sweep_state row before it can be marked sold
    # (mark_sold only UPDATEs an existing row) — mirrors the real
    # run_sweep -> sell_arrived -> mark_sold lifecycle.
    store.record_sweep(
        position_id=positions[0]["id"],
        status=SweepStatus.SHARE_ARRIVED,
        observed_qty=1,
        expected_post_qty=1,
        observed_at="2026-05-20T00:00:00",
    )
    store.mark_sold(positions[0]["id"], sold_at="2026-06-01T00:00:00")
    store.close()

    second = _run(router.record_rsa_trade(
        ticker="AREB", split_ratio="1:25", execution=execution
    ))
    assert second["ok"] is True
    assert second["trade_id"] != first["trade_id"]

    store = RsaStore(str(db_path))
    trades = store.list_trades()
    store.close()
    assert len(trades) == 2


def _run(coro):
    import asyncio

    return asyncio.run(coro)
