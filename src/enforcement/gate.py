from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from enforcement.account_checks import (
    DEFAULT_RECONCILIATION_EPSILON,
    check_reconciliation,
    filter_eligible_accounts,
)
from enforcement.audit_log import AuditEntry, AuditLog
from enforcement.circuit_breaker import CircuitBreaker
from enforcement.errors import (
    GateError,
    LiveOrderRequiresConfirmation,
)
from enforcement.freeze import check_freeze
from enforcement.intent import idempotency_key, intent_hash
from enforcement.limits import (
    check_daily_limit,
    check_per_order_limit,
    estimate_usd,
)
from enforcement.propose_execute import (
    ProposalStore,
    propose_fanout as _propose_fanout,
    validate_leg_for_execute,
)
from enforcement.types import (
    AccountStatusProvider,
    BrokerAccount,
    GateDecision,
    LegProposal,
    OrderIntent,
    Proposal,
)

DEFAULT_PROPOSAL_TTL_SECONDS = 300.0


def _default_state_dir() -> Path:
    return Path(os.getenv("SSG_STATE_DIR", "logs"))


@dataclass
class EnforcementCore:
    """Composition root for the enforcement library.

    A single instance is shared across CLI, TUI, and the router MCP. Per-broker
    MCPs construct their own instance bound to the same SQLite/audit paths so
    they can independently re-validate (defense in depth).
    """

    proposal_store: ProposalStore
    audit: AuditLog
    breaker: CircuitBreaker
    proposal_ttl_seconds: float = DEFAULT_PROPOSAL_TTL_SECONDS
    reconciliation_epsilon: float = DEFAULT_RECONCILIATION_EPSILON
    _initialized: bool = field(default=True, init=False, repr=False)

    @classmethod
    def from_default_paths(cls) -> "EnforcementCore":
        d = _default_state_dir()
        d.mkdir(parents=True, exist_ok=True)
        return cls(
            proposal_store=ProposalStore(d / "proposals.sqlite"),
            audit=AuditLog(d / "audit.jsonl"),
            breaker=CircuitBreaker(),
        )

    def propose_order(
        self,
        intent: OrderIntent,
        provider: AccountStatusProvider,
        *,
        ref_price: float,
        stored_qty_by_account: dict[BrokerAccount, float] | None = None,
        force_reconcile: bool = False,
    ) -> tuple[Proposal, GateDecision]:
        """Run the full safety pipeline, then mint a proposal token.

        Returns (proposal, decision). decision.skipped_brokers names any
        accounts dropped from the fan-out for settled-cash/PDT reasons; the
        proposal binds to the FILTERED intent so execute() routes only to
        eligible accounts.
        """
        normalized = intent.normalized()
        try:
            check_freeze(normalized.ticker)
            for account in normalized.targets:
                self.breaker.check(account.broker)
            kept, skipped = filter_eligible_accounts(normalized, provider)
            if not kept:
                raise GateError(
                    "no eligible accounts after settled-cash/PDT filtering; "
                    "nothing to propose"
                )
            filtered = OrderIntent(
                ticker=normalized.ticker,
                side=normalized.side,
                qty=normalized.qty,
                targets=kept,
                price=normalized.price,
                dry_run=normalized.dry_run,
            )
            check_reconciliation(
                filtered,
                provider,
                stored_qty_by_account or {},
                epsilon=self.reconciliation_epsilon,
                force=force_reconcile,
            )
            estimated = estimate_usd(filtered, ref_price)
            check_per_order_limit(estimated)
            check_daily_limit(estimated, self.audit)
            self.audit.require_intact()
            unit_cost = (
                float(filtered.price) if filtered.price is not None else float(ref_price)
            ) * float(filtered.qty)
            estimated_per_leg = {target: unit_cost for target in filtered.targets}
            proposal = _propose_fanout(
                intent=filtered,
                estimated_usd_total=estimated,
                estimated_usd_per_leg=estimated_per_leg,
                store=self.proposal_store,
                ttl_seconds=self.proposal_ttl_seconds,
            )
            self.audit.append(
                AuditEntry(
                    ts="",
                    kind="propose",
                    intent_hash=None,
                    token=proposal.proposal_id,
                    dry_run=filtered.dry_run,
                    result="ok",
                    extra={
                        "ticker": filtered.ticker,
                        "side": filtered.side.value,
                        "qty": filtered.qty,
                        "targets": [list(t.as_tuple()) for t in filtered.targets],
                        "estimated_usd": estimated,
                        "ttl_seconds": self.proposal_ttl_seconds,
                        "skipped_brokers": skipped,
                        "leg_count": proposal.leg_count,
                        "leg_tokens": [leg.token for leg in proposal.legs],
                    },
                )
            )
            return proposal, GateDecision(
                allowed=True,
                idempotency_key=None,
                skipped_brokers=skipped,
            )
        except GateError as e:
            self.audit.append(
                AuditEntry(
                    ts="",
                    kind="propose",
                    intent_hash=intent_hash(normalized),
                    dry_run=normalized.dry_run,
                    result="rejected",
                    reason=e.reason,
                    extra={
                        "ticker": normalized.ticker,
                        "side": normalized.side.value,
                        "message": str(e),
                    },
                )
            )
            raise

    def gate_execute_leg(
        self,
        leg_token: str,
        intent: OrderIntent,
        leg: BrokerAccount,
    ) -> tuple[GateDecision, LegProposal | None]:
        """Per-broker-leg validation. Called by each per-broker MCP before placing.

        - For live orders (dry_run=False): the broker MUST supply a valid
          leg_token. The intent supplied is the broker's single-target intent
          (`targets=(self_leg,)`). validate_leg_for_execute consumes the leg
          token atomically and confirms the leg's bound intent_hash matches.
        - For dry-run orders: leg_token is optional; if supplied it's
          validated, otherwise a synthetic idempotency_key is derived.
        """
        normalized = intent.normalized()
        consumed: LegProposal | None = None
        if not normalized.dry_run:
            if not leg_token:
                raise LiveOrderRequiresConfirmation(
                    "live leg requires a leg confirmation_token from a prior "
                    "propose_order call"
                )
            consumed = validate_leg_for_execute(
                leg_token, normalized, store=self.proposal_store
            )
        elif leg_token:
            consumed = validate_leg_for_execute(
                leg_token, normalized, store=self.proposal_store
            )
        self.breaker.check(leg.broker)
        key = idempotency_key(leg_token or "dryrun", leg.broker, leg.account_id)
        return GateDecision(allowed=True, idempotency_key=key), consumed

    def record_leg_outcome(
        self,
        *,
        token: str,
        intent: OrderIntent,
        leg: BrokerAccount,
        idempotency_key_value: str,
        result: str,
        reason: str | None = None,
        usd_amount: float | None = None,
        fill_qty: float | None = None,
        fill_price: float | None = None,
    ) -> None:
        normalized = intent.normalized()
        is_success = result == "ok"
        if is_success:
            self.breaker.record_success(leg.broker)
        else:
            self.breaker.record_failure(leg.broker, reason or "unknown")
        self.audit.append(
            AuditEntry(
                ts="",
                kind="execute",
                intent_hash=intent_hash(normalized),
                token=token or None,
                broker=leg.broker,
                account_id=leg.account_id,
                dry_run=normalized.dry_run,
                result=result,
                reason=reason,
                extra={
                    "ticker": normalized.ticker,
                    "side": normalized.side.value,
                    "qty": normalized.qty,
                    "idempotency_key": idempotency_key_value,
                    "usd_amount": usd_amount,
                    "fill_qty": fill_qty,
                    "fill_price": fill_price,
                },
            )
        )


def gate_order(
    core: EnforcementCore,
    intent: OrderIntent,
    provider: AccountStatusProvider,
    *,
    ref_price: float,
    stored_qty_by_account: dict[BrokerAccount, float] | None = None,
    force_reconcile: bool = False,
) -> tuple[Proposal, GateDecision]:
    """Module-level convenience: equivalent to `core.propose_order(...)`.

    CLI/TUI/router-MCP/broker-MCP all import THIS function. The presence of a
    `gate_order` call site immediately preceding every broker call is what the
    F5 static check enforces; that's why the public API is module-level.
    """
    return core.propose_order(
        intent,
        provider,
        ref_price=ref_price,
        stored_qty_by_account=stored_qty_by_account,
        force_reconcile=force_reconcile,
    )
