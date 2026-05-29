from __future__ import annotations

from enforcement.errors import ReconciliationDivergence
from enforcement.types import AccountStatusProvider, BrokerAccount, OrderIntent, OrderSide

DEFAULT_RECONCILIATION_EPSILON = 0.0
"""Default qty divergence permitted between stored RsaStore positions and live
broker holdings before pre-buy reconciliation fails. 0.0 = strict equality."""


def check_reconciliation(
    intent: OrderIntent,
    provider: AccountStatusProvider,
    stored_qty_by_account: dict[BrokerAccount, float],
    *,
    epsilon: float = DEFAULT_RECONCILIATION_EPSILON,
    force: bool = False,
) -> list[tuple[BrokerAccount, float, float]]:
    """Return the list of (account, stored, observed) that diverge beyond
    epsilon. If any diverge and `force=False`, raise.

    Only fires on buys — sells against a known position do not require pre-buy
    reconciliation (the sell path has its own settled-cash + observed_qty path).
    """
    if intent.side != OrderSide.BUY:
        return []
    diverged: list[tuple[BrokerAccount, float, float]] = []
    for account in intent.targets:
        stored = float(stored_qty_by_account.get(account, 0.0))
        observed = float(provider.get_observed_qty(account.broker, account.account_id, intent.ticker))
        if abs(observed - stored) > epsilon:
            diverged.append((account, stored, observed))
    if diverged and not force:
        sample = ", ".join(
            f"{a.broker}/{a.account_id}: stored={s} observed={o}"
            for a, s, o in diverged[:3]
        )
        more = f" (+{len(diverged) - 3} more)" if len(diverged) > 3 else ""
        raise ReconciliationDivergence(
            f"position state diverged from broker-reported holdings; "
            f"{sample}{more}. Re-run with --force-reconcile to override."
        )
    return diverged


def filter_eligible_accounts(
    intent: OrderIntent,
    provider: AccountStatusProvider,
) -> tuple[tuple[BrokerAccount, ...], list[tuple[str, str, str]]]:
    """Return (kept, skipped). `skipped` is a list of (broker, account_id, reason)
    explaining why each excluded account was dropped from the fan-out.

    For BUYs: skip accounts with settled_cash < estimated leg cost OR at the
    day-trade limit (5 within a 5-business-day window).
    For SELLs: no skipping here — the proposal estimate already assumes the
    qty is available; mismatch surfaces at execute via the broker's response.
    """
    if intent.side != OrderSide.BUY:
        return intent.targets, []

    unit_price = intent.price if intent.price is not None else 0.0
    per_leg_cost = float(intent.qty) * float(unit_price)
    kept: list[BrokerAccount] = []
    skipped: list[tuple[str, str, str]] = []
    for account in intent.targets:
        broker, account_id = account.as_tuple()
        try:
            settled = float(provider.get_settled_cash(broker, account_id))
        except Exception as e:
            skipped.append((broker, account_id, f"settled_cash_unavailable: {e}"))
            continue
        if unit_price > 0 and settled < per_leg_cost:
            skipped.append(
                (broker, account_id, f"insufficient_settled_cash:{settled:.2f}<{per_leg_cost:.2f}")
            )
            continue
        try:
            day_trades = int(provider.get_day_trades_in_window(broker, account_id))
        except Exception as e:
            skipped.append((broker, account_id, f"pdt_status_unavailable: {e}"))
            continue
        if day_trades >= 4:
            skipped.append((broker, account_id, f"pdt_limit_reached:{day_trades}/4"))
            continue
        kept.append(account)
    return tuple(kept), skipped
