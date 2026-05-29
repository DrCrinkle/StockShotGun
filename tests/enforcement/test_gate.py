from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from enforcement import (
    AuditLog,
    BrokerAccount,
    CircuitOpen,
    DailyLimitExceeded,
    EnforcementCore,
    FrozenTicker,
    IntentMismatch,
    LiveOrderRequiresConfirmation,
    OrderIntent,
    OrderSide,
    PerOrderLimitExceeded,
    ProposalStore,
    TokenAlreadyUsed,
    TokenExpired,
    gate_order,
    intent_hash,
)
from enforcement.audit_log import AuditEntry
from enforcement.circuit_breaker import CircuitBreaker


class FakeProvider:
    def __init__(self, settled: float = 10_000.0, day_trades: int = 0, observed: float = 0.0):
        self.settled = settled
        self.day_trades = day_trades
        self.observed = observed

    def get_settled_cash(self, broker: str, account_id: str) -> float:
        return self.settled

    def get_day_trades_in_window(self, broker: str, account_id: str) -> int:
        return self.day_trades

    def get_observed_qty(self, broker: str, account_id: str, ticker: str) -> float:
        return self.observed


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=2, cooldown_seconds=10.0),
        proposal_ttl_seconds=5.0,
    )


def _buy_intent(qty: float = 10.0, price: float | None = 5.0, dry_run: bool = True) -> OrderIntent:
    return OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=qty,
        targets=(BrokerAccount("Robinhood", "acc1"),),
        price=price,
        dry_run=dry_run,
    )


def test_propose_returns_per_leg_tokens(core: EnforcementCore):
    intent = _buy_intent()
    proposal, decision = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    assert proposal.proposal_id
    assert proposal.leg_count == 1
    leg = proposal.legs[0]
    assert leg.token
    # Per-leg hash is for a SINGLE-target intent, so matches a rebuilt single-leg
    single_leg_intent = _buy_intent()  # already single-target
    assert leg.intent_hash == intent_hash(single_leg_intent.normalized())
    assert decision.allowed is True


def test_intent_mismatch_between_propose_and_execute_is_rejected(core: EnforcementCore):
    proposed = _buy_intent(qty=10.0, dry_run=False)
    different = _buy_intent(qty=11.0, dry_run=False)
    proposal, _ = gate_order(core, proposed, FakeProvider(), ref_price=5.0)
    leg_token = proposal.legs[0].token
    with pytest.raises(IntentMismatch):
        core.gate_execute_leg(
            leg_token=leg_token,
            intent=different,
            leg=BrokerAccount("Robinhood", "acc1"),
        )


def test_token_is_single_use(core: EnforcementCore):
    intent = _buy_intent(dry_run=False)
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    leg = BrokerAccount("Robinhood", "acc1")
    leg_token = proposal.legs[0].token
    core.gate_execute_leg(leg_token=leg_token, intent=intent, leg=leg)
    with pytest.raises(TokenAlreadyUsed):
        core.gate_execute_leg(leg_token=leg_token, intent=intent, leg=leg)


def test_token_expires(core: EnforcementCore):
    core.proposal_ttl_seconds = 0.01
    intent = _buy_intent(dry_run=False)
    proposal, _ = gate_order(core, intent, FakeProvider(), ref_price=5.0)
    time.sleep(0.05)
    with pytest.raises(TokenExpired):
        core.gate_execute_leg(
            leg_token=proposal.legs[0].token,
            intent=intent,
            leg=BrokerAccount("Robinhood", "acc1"),
        )


def test_live_order_without_token_rejected(core: EnforcementCore):
    intent = _buy_intent(dry_run=False)
    with pytest.raises(LiveOrderRequiresConfirmation):
        core.gate_execute_leg(
            leg_token="",
            intent=intent,
            leg=BrokerAccount("Robinhood", "acc1"),
        )


def test_per_order_limit_enforced(core: EnforcementCore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SSG_MAX_ORDER_USD", "10")
    intent = _buy_intent(qty=10.0, price=5.0)
    with pytest.raises(PerOrderLimitExceeded):
        gate_order(core, intent, FakeProvider(), ref_price=5.0)


def test_daily_limit_enforced(core: EnforcementCore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SSG_MAX_DAILY_USD", "200")
    core.audit.append(
        AuditEntry(
            ts="",
            kind="execute",
            dry_run=False,
            result="ok",
            extra={"usd_amount": 150.0},
        )
    )
    intent = _buy_intent(qty=20.0, price=5.0)
    with pytest.raises(DailyLimitExceeded):
        gate_order(core, intent, FakeProvider(), ref_price=5.0)


def test_frozen_ticker_rejected(core: EnforcementCore, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SSG_FROZEN_TICKERS", "TSLA,GME")
    with pytest.raises(FrozenTicker):
        gate_order(core, _buy_intent(), FakeProvider(), ref_price=5.0)


def test_circuit_breaker_opens_after_threshold(core: EnforcementCore):
    core.breaker.record_failure("Robinhood", "network")
    core.breaker.record_failure("Robinhood", "network")
    with pytest.raises(CircuitOpen):
        gate_order(core, _buy_intent(), FakeProvider(), ref_price=5.0)


def test_audit_chain_verifies(core: EnforcementCore):
    intent = _buy_intent()
    gate_order(core, intent, FakeProvider(), ref_price=5.0)
    gate_order(core, intent, FakeProvider(), ref_price=5.0)
    ok, count, msg = core.audit.verify()
    assert ok, msg
    assert count >= 2


def test_audit_chain_tamper_detected(core: EnforcementCore):
    gate_order(core, _buy_intent(), FakeProvider(), ref_price=5.0)
    raw = core.audit.path.read_text(encoding="utf-8")
    # Flip a single character inside the JSON payload to break the chain
    tampered = raw.replace('"kind":"propose"', '"kind":"PROPOSE"', 1)
    core.audit.path.write_text(tampered, encoding="utf-8")
    ok, _, msg = core.audit.verify()
    assert not ok
    assert msg is not None


def test_dry_run_does_not_require_token(core: EnforcementCore):
    intent = _buy_intent(dry_run=True)
    decision, _ = core.gate_execute_leg(
        leg_token="",
        intent=intent,
        leg=BrokerAccount("Robinhood", "acc1"),
    )
    assert decision.allowed
    assert decision.idempotency_key is not None
