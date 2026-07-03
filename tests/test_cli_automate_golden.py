"""ADR-0006 Task 4 golden tests for the `automate` path
(`cli.automate._run_automate_from_recap`).

Pins the same engine-native contract as tests/test_cli_batch_golden.py:
propose_order + execute_order looped per generated order (buy signals due
today + pending sell triggers), fail-fast on the first GateError, and an
execute-time rejection raising CliRuntimeError(FULL_BROKER_FAILURE) instead
of rendering as success.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import cli.automate as automate_mod
from cli.automate import _run_automate_from_recap
from cli_runtime import ExitCode
from enforcement import GateError


def _run(coro):
    return asyncio.run(coro)


async def _some_trade(*a):
    return True


@pytest.fixture
def one_broker(monkeypatch):
    monkeypatch.setattr(
        automate_mod,
        "BROKER_FUNCTIONS",
        {"Public": {"trade": _some_trade, "holdings": _some_holdings}},
    )


async def _some_holdings(ticker=None):
    return {ticker or "ALL": 5}


@pytest.fixture
def stub_session_init(monkeypatch):
    async def fake_init(brokers):
        return None

    monkeypatch.setattr(
        automate_mod, "session_manager",
        SimpleNamespace(initialize_selected_sessions=fake_init),
    )


@pytest.fixture
def stub_default_brokers(monkeypatch):
    monkeypatch.setattr(automate_mod, "_default_brokers_for_trade", lambda: ["Public"])


def _ctx(**over):
    base = dict(output_format="json", mock_brokers=False, dry_run=False)
    base.update(over)
    return SimpleNamespace(**base)


class _StubEngine:
    def __init__(self, proposals=None, executions=None, propose_side_effects=None):
        self.propose_calls: list[dict] = []
        self.execute_calls: list[dict] = []
        self._proposals = list(proposals or [])
        self._executions = list(executions or [])
        self._propose_side_effects = list(propose_side_effects or [])
        self._propose_idx = 0
        self._execute_idx = 0

    async def validate_targets(self, **kwargs):
        return list(kwargs["selected_brokers"]), []

    async def propose_order(self, **kwargs):
        self.propose_calls.append(kwargs)
        idx = self._propose_idx
        self._propose_idx += 1
        if idx < len(self._propose_side_effects) and self._propose_side_effects[idx] is not None:
            raise self._propose_side_effects[idx]
        return self._proposals[idx]

    async def execute_order(self, **kwargs):
        self.execute_calls.append(kwargs)
        idx = self._execute_idx
        self._execute_idx += 1
        return self._executions[idx]


def _proposal(pid: str, leg_count: int = 1) -> dict:
    return {
        "proposal_id": pid,
        "estimated_usd": 1.0,
        "leg_count": leg_count,
        "accounts_by_broker": {"Public": ["primary"]},
        "skipped_brokers": [],
    }


def _execution(pid: str, ticker: str, side: str, broker: str = "Public", ok: bool = True) -> dict:
    return {
        "proposal_id": pid,
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "dry_run": False,
        "results": [
            {
                "broker": broker,
                "account_id": "primary",
                "ok": ok,
                "dry_run": False,
                "idempotency_key": f"k-{pid}",
                "reason": None if ok else "boom",
                "detail": "placed" if ok else "broker error",
            }
        ],
        "success_count": 1 if ok else 0,
        "failure_count": 0 if ok else 1,
    }


@pytest.fixture
def recap_file(tmp_path):
    path = tmp_path / "recap.txt"
    path.write_text("no signals here", encoding="utf-8")
    return str(path)


@pytest.fixture
def stub_recap_parsing(monkeypatch):
    """Feed one due buy signal through parse_chat_recap + AutomationRecapStore
    without touching sqlite disk state beyond a tmp db path."""

    def fake_parse_chat_recap(text):
        return [], []

    monkeypatch.setattr(automate_mod, "parse_chat_recap", fake_parse_chat_recap)


class _FakeStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self.closed = False
        self.marked_buys: list[int] = []
        self.marked_sells: list[int] = []

    def record_recap(self, recap_text, upcoming, stock_back, now):
        return {"recap_id": 1}

    def get_due_buy_signals(self, today_date):
        return [{"id": 1, "ticker": "TSLA"}]

    def get_pending_sell_triggers(self):
        return []

    def mark_buy_signals_executed(self, ids, now):
        self.marked_buys = list(ids)

    def mark_sell_triggers_executed(self, ids, now):
        self.marked_sells = list(ids)

    def close(self):
        self.closed = True


@pytest.fixture
def fake_store(monkeypatch):
    store_holder = {}

    def factory(db_path):
        store = _FakeStore(db_path)
        store_holder["store"] = store
        return store

    monkeypatch.setattr(automate_mod, "AutomationRecapStore", factory)
    return store_holder


def _args(recap_file, db_path):
    return SimpleNamespace(
        recap_file=recap_file,
        db_path=db_path,
        today_mmdd=None,
        default_qty=1,
        broker=None,
    )


# --------------------------------------------------------------------------
# Engine wiring — one due buy signal proposes + executes on the engine.
# --------------------------------------------------------------------------
def test_automate_proposes_and_executes_due_buy_on_the_engine(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    engine = _StubEngine(
        proposals=[_proposal("p1")],
        executions=[_execution("p1", "TSLA", "buy")],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    exit_code, data = _run(
        _run_automate_from_recap(args, parser=None, context=_ctx())
    )

    assert len(engine.propose_calls) == 1
    assert engine.propose_calls[0]["dry_run"] is False
    assert len(engine.execute_calls) == 1
    assert engine.execute_calls[0]["dry_run"] is False
    assert exit_code == ExitCode.SUCCESS
    assert data["results"]["successful"] == 1
    assert data["executed_buy_signals"] == [1]
    assert fake_store["store"].marked_buys == [1]


# --------------------------------------------------------------------------
# Fail-fast on first GateError — aborts the whole automation batch.
# --------------------------------------------------------------------------
def test_automate_aborts_wholesale_on_first_gate_rejection(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    gate_err = GateError("frozen ticker")
    gate_err.reason = "frozen_ticker"  # type: ignore[attr-defined]
    engine = _StubEngine(propose_side_effects=[gate_err])

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(automate_mod, "gate_error_to_exit_code", lambda e: 2)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    with pytest.raises(automate_mod.CliRuntimeError) as excinfo:
        _run(_run_automate_from_recap(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.INVALID_ARGS
    assert len(engine.execute_calls) == 0
    # Nothing marked executed — the batch never reached fan-out.
    assert fake_store["store"].marked_buys == []


# --------------------------------------------------------------------------
# Execute-time rejection must NOT read as success.
# --------------------------------------------------------------------------
def test_automate_execute_rejection_raises_full_broker_failure(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    rejection = {
        "proposal_id": "p1",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id p1",
    }
    engine = _StubEngine(proposals=[_proposal("p1")], executions=[rejection])

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    with pytest.raises(automate_mod.CliRuntimeError) as excinfo:
        _run(_run_automate_from_recap(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.FULL_BROKER_FAILURE
    assert excinfo.value.exit_code != ExitCode.SUCCESS
    # Nothing marked executed on rejection.
    assert fake_store["store"].marked_buys == []


# --------------------------------------------------------------------------
# --dry-run is now a full-pipeline rehearsal (mirrors trade.py Task 3).
# --------------------------------------------------------------------------
def test_automate_dry_run_is_full_pipeline_rehearsal(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    engine = _StubEngine(
        proposals=[_proposal("p1")],
        executions=[
            {
                "proposal_id": "p1",
                "ticker": "TSLA",
                "side": "buy",
                "qty": 1,
                "dry_run": True,
                "results": [
                    {"broker": "Public", "account_id": "primary", "ok": True,
                     "dry_run": True, "idempotency_key": "k1", "reason": None,
                     "detail": "dry-run ok"},
                ],
                "success_count": 1,
                "failure_count": 0,
            }
        ],
    )

    async def fake_get_engine():
        return engine

    trade_called: list = []

    async def spy_trade(*a, **kw):
        trade_called.append((a, kw))
        return True

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)
    monkeypatch.setattr(
        automate_mod, "BROKER_FUNCTIONS",
        {"Public": {"trade": spy_trade, "holdings": _some_holdings}},
    )

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    exit_code, data = _run(
        _run_automate_from_recap(args, parser=None, context=_ctx(dry_run=True))
    )

    assert len(engine.propose_calls) == 1
    assert engine.propose_calls[0]["dry_run"] is True
    assert len(engine.execute_calls) == 1
    assert engine.execute_calls[0]["dry_run"] is True
    assert trade_called == []
    assert exit_code == ExitCode.SUCCESS
    assert data["results"]["successful"] == 1
