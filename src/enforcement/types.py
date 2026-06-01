from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class BrokerAccount:
    broker: str
    account_id: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.broker, self.account_id)


@dataclass(frozen=True)
class OrderIntent:
    """Normalized order intent — hashed into the proposal token binding.

    A confirmation_token authorizes exactly the OrderIntent whose hash it was
    bound to. Any field difference between propose() and execute() invalidates
    the token. price=None means market order; price=float means limit order.
    """

    ticker: str
    side: OrderSide
    qty: float
    targets: tuple[BrokerAccount, ...]
    price: float | None = None
    dry_run: bool = True

    def normalized(self) -> "OrderIntent":
        return OrderIntent(
            ticker=self.ticker.upper().strip(),
            side=OrderSide(self.side),
            qty=float(self.qty),
            targets=tuple(sorted(self.targets, key=lambda b: b.as_tuple())),
            price=None if self.price is None else float(self.price),
            dry_run=bool(self.dry_run),
        )


@dataclass(frozen=True)
class LegProposal:
    """A single-leg sub-proposal — one per (broker, account_id) in a fan-out.

    Each leg carries its own single-use token, bound to a single-target intent.
    A broker MCP (in-process or subprocess) validates THIS token against its
    own single-target intent at execute time — no router-validated escape hatch
    needed. Single-use is enforced per-leg, so a token replay against any
    individual broker is rejected regardless of what happened at sibling legs.
    """

    token: str
    intent_hash: str
    broker: str
    account_id: str
    proposal_id: str
    valid_until_ts: float
    estimated_usd: float
    created_ts: float


@dataclass(frozen=True)
class Proposal:
    """A fan-out proposal: N leg sub-proposals bound to a master proposal_id.

    The `proposal_id` is what the agent passes back to execute_order. The
    router looks up the constituent legs by proposal_id and dispatches each
    broker call with the leg's token. The agent never sees the leg tokens.
    """

    proposal_id: str
    legs: tuple[LegProposal, ...]
    valid_until_ts: float
    estimated_usd: float
    created_ts: float
    # The order parameters this proposal authorizes. Stored so execute_order is
    # self-sufficient from the durable store — no router-side intent cache. They
    # are uniform across all legs by construction (propose_fanout builds every
    # leg from one OrderIntent); the per-leg `intent_hash` remains the security
    # authority, so these are a faithful description, not a trust boundary.
    ticker: str
    side: OrderSide
    qty: float
    price: float | None
    dry_run: bool

    @property
    def leg_count(self) -> int:
        return len(self.legs)


@dataclass
class GateDecision:
    """Result of `gate_order()`. allowed=False means the broker call MUST NOT
    happen; rejections always include a reason and a human-readable message."""

    allowed: bool
    reason: str | None = None
    message: str | None = None
    idempotency_key: str | None = None
    skipped_brokers: list[tuple[str, str, str]] = field(default_factory=list)
    """List of (broker, account_id, reason) skipped from a fan-out without halting the rest."""


class AccountStatusProvider(Protocol):
    """Callback interface broker MCPs implement. Enforcement does not call
    brokers directly; it asks the provider for the fields it needs to gate."""

    def get_settled_cash(self, broker: str, account_id: str) -> float: ...

    def get_day_trades_in_window(self, broker: str, account_id: str) -> int: ...

    def get_observed_qty(self, broker: str, account_id: str, ticker: str) -> float: ...
