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


def _multi_leg_execution(
    pid: str,
    ticker: str,
    side: str,
    legs: list[tuple[str, str, bool]],
) -> dict:
    """Engine execution whose legs carry REAL account ids (broker, account_id,
    ok) — the multi-account fan-out shape (ADR 0001) that broke completion
    tracking (final-review C2: labels vs bare broker names)."""
    return {
        "proposal_id": pid,
        "ticker": ticker,
        "side": side,
        "qty": 1,
        "dry_run": False,
        "results": [
            {
                "broker": broker,
                "account_id": account_id,
                "ok": ok,
                "dry_run": False,
                "idempotency_key": f"k-{pid}-{account_id}",
                "reason": None if ok else "boom",
                "detail": "placed" if ok else "broker error",
            }
            for broker, account_id, ok in legs
        ],
        "success_count": sum(1 for *_x, ok in legs if ok),
        "failure_count": sum(1 for *_x, ok in legs if not ok),
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
        # Real store's mark_*_executed is a per-call UPDATE ... WHERE id IN
        # (...) — additive across calls, not a wholesale replace. Accumulate
        # here so incremental (per-order) marking is observable in tests.
        if not ids:
            return
        for signal_id in ids:
            if signal_id not in self.marked_buys:
                self.marked_buys.append(signal_id)

    def mark_sell_triggers_executed(self, ids, now):
        if not ids:
            return
        for trigger_id in ids:
            if trigger_id not in self.marked_sells:
                self.marked_sells.append(trigger_id)

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


class _TwoBuysFakeStore(_FakeStore):
    """Two due buy signals — used to test incremental per-order marking on a
    mid-batch execute rejection."""

    def get_due_buy_signals(self, today_date):
        return [{"id": 1, "ticker": "TSLA"}, {"id": 2, "ticker": "AAPL"}]


@pytest.fixture
def fake_store_two_buys(monkeypatch):
    store_holder = {}

    def factory(db_path):
        store = _TwoBuysFakeStore(db_path)
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
    # Dry-run rehearsal must not mark anything executed — no orders were
    # actually placed anywhere.
    assert fake_store["store"].marked_buys == []
    assert fake_store["store"].marked_sells == []


# --------------------------------------------------------------------------
# Mid-batch execute rejection: order 1 already executed successfully before
# order 2's execution comes back rejected. This must NOT be a double-trade
# risk on the next automate run — order 1's buy signal must already be
# marked executed by the time the CliRuntimeError is raised, and the
# rejection's details must carry order 1's completed rendered results.
# --------------------------------------------------------------------------
def test_automate_marks_completed_signal_before_mid_batch_abort(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store_two_buys, recap_file, monkeypatch,
):
    rejection = {
        "proposal_id": "p2",
        "dry_run": False,
        "results": [],
        "success_count": 0,
        "failure_count": 0,
        "rejected": True,
        "reason": "proposal_not_found",
        "detail": "no proposal with id p2",
    }
    engine = _StubEngine(
        proposals=[_proposal("p1"), _proposal("p2")],
        executions=[
            _execution("p1", "TSLA", "buy", ok=True),
            rejection,
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    with pytest.raises(automate_mod.CliRuntimeError) as excinfo:
        _run(_run_automate_from_recap(args, parser=None, context=_ctx()))

    assert excinfo.value.exit_code == ExitCode.FULL_BROKER_FAILURE
    details = excinfo.value.details
    assert details["completed_orders"] == 1
    assert details["completed_results"]["successful"] >= 1
    assert details["completed_results"]["failed"] == 0

    store = fake_store_two_buys["store"]
    # Order 1's signal (id 1) already executed live — must be marked so it
    # doesn't re-execute on the next automate run. Order 2's signal (id 2)
    # never executed — must NOT be marked.
    assert store.marked_buys == [1]
    assert 2 not in store.marked_buys


# --------------------------------------------------------------------------
# Final-review M4 follow-up: unlike batch.py (which printed the DRY RUN
# banner twice in text mode — header block + response fn), automate.py emits
# it through `automation_response_fn` only. Pin the single occurrence so a
# future header block doesn't reintroduce the batch bug here.
# --------------------------------------------------------------------------
def test_automate_dry_run_banner_prints_once_in_text_mode(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch, capsys,
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

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    _run(
        _run_automate_from_recap(
            args, parser=None, context=_ctx(dry_run=True, output_format="text")
        )
    )

    out = capsys.readouterr().out
    banner = "DRY RUN — full pipeline rehearsal, no orders placed"
    assert out.count(banner) == 1, out


# --------------------------------------------------------------------------
# Final-review C2: completion tracking must compare bare broker names from
# the RAW execution legs, not rendered `_leg_label` output ("Broker:acct").
# With per-account fan-out (real account ids on the legs), the rendered
# labels never equal the expected broker names, so signals were never marked
# executed and re-executed on every automate run — a double-trade bug.
# --------------------------------------------------------------------------
def test_automate_marks_signal_executed_when_legs_carry_real_account_ids(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    """Multi-account fan-out: both of Public's legs succeed under real
    account ids. The buy signal MUST be marked executed — the broker
    completed, regardless of how its legs render."""
    engine = _StubEngine(
        proposals=[_proposal("p1", leg_count=2)],
        executions=[
            _multi_leg_execution(
                "p1", "TSLA", "buy",
                legs=[("Public", "acctA", True), ("Public", "acctB", True)],
            )
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    exit_code, data = _run(
        _run_automate_from_recap(args, parser=None, context=_ctx())
    )

    assert exit_code == ExitCode.SUCCESS
    assert data["executed_buy_signals"] == [1]
    assert fake_store["store"].marked_buys == [1]


def test_automate_does_not_mark_signal_when_all_real_account_legs_failed(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    """Inverse: every leg failed → the broker did NOT complete → the signal
    must stay pending (it should re-execute on the next automate run)."""
    engine = _StubEngine(
        proposals=[_proposal("p1", leg_count=2)],
        executions=[
            _multi_leg_execution(
                "p1", "TSLA", "buy",
                legs=[("Public", "acctA", False), ("Public", "acctB", False)],
            )
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    exit_code, data = _run(
        _run_automate_from_recap(args, parser=None, context=_ctx())
    )

    assert data["executed_buy_signals"] == []
    assert fake_store["store"].marked_buys == []


def test_automate_marks_signal_when_at_least_one_leg_succeeded(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store, recap_file, monkeypatch,
):
    """Decided semantics (final-review C2): a broker counts as completed when
    >= 1 of its legs succeeded — parity with the old internal-fan-out
    behavior, where the broker call's overall success (Fennel: 'True if at
    least one account succeeded') marked the signal."""
    engine = _StubEngine(
        proposals=[_proposal("p1", leg_count=2)],
        executions=[
            _multi_leg_execution(
                "p1", "TSLA", "buy",
                legs=[("Public", "acctA", True), ("Public", "acctB", False)],
            )
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    _run(_run_automate_from_recap(args, parser=None, context=_ctx()))

    assert fake_store["store"].marked_buys == [1]


# --------------------------------------------------------------------------
# All-success path: same marks, same timing — bulk marking after the loop
# and incremental marking inside the loop must agree when nothing aborts.
# --------------------------------------------------------------------------
def test_automate_marks_all_signals_executed_on_full_success(
    tmp_path, one_broker, stub_session_init, stub_default_brokers,
    stub_recap_parsing, fake_store_two_buys, recap_file, monkeypatch,
):
    engine = _StubEngine(
        proposals=[_proposal("p1"), _proposal("p2")],
        executions=[
            _execution("p1", "TSLA", "buy", ok=True),
            _execution("p2", "AAPL", "buy", ok=True),
        ],
    )

    async def fake_get_engine():
        return engine

    monkeypatch.setattr(automate_mod, "get_engine", fake_get_engine)

    args = _args(recap_file, str(tmp_path / "automation.sqlite"))
    exit_code, data = _run(
        _run_automate_from_recap(args, parser=None, context=_ctx())
    )

    assert exit_code == ExitCode.SUCCESS
    assert data["executed_buy_signals"] == [1, 2]
    store = fake_store_two_buys["store"]
    assert sorted(store.marked_buys) == [1, 2]
