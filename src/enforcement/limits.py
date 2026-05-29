from __future__ import annotations

import os

from enforcement.audit_log import AuditLog
from enforcement.errors import DailyLimitExceeded, PerOrderLimitExceeded
from enforcement.types import OrderIntent

DEFAULT_MAX_ORDER_USD = 500.0
DEFAULT_MAX_DAILY_USD = 2000.0


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def per_order_limit() -> float:
    return _env_float("SSG_MAX_ORDER_USD", DEFAULT_MAX_ORDER_USD)


def per_day_limit() -> float:
    return _env_float("SSG_MAX_DAILY_USD", DEFAULT_MAX_DAILY_USD)


def estimate_usd(intent: OrderIntent, ref_price: float) -> float:
    """Sum the estimated USD across all targets. For market orders the caller
    supplies a reference price (last quote); for limit orders intent.price is
    authoritative. Either way the SAME estimate must be passed to propose()
    so the per-day-limit calculation is consistent across propose and execute.
    """
    unit_price = intent.price if intent.price is not None else ref_price
    return float(intent.qty) * float(unit_price) * len(intent.targets)


def check_per_order_limit(estimated: float) -> None:
    cap = per_order_limit()
    if estimated > cap:
        raise PerOrderLimitExceeded(
            f"order estimate ${estimated:.2f} exceeds per-order cap ${cap:.2f}"
        )


def check_daily_limit(estimated: float, audit: AuditLog) -> None:
    cap = per_day_limit()
    spent_today = audit.sum_executed_usd_today()
    if spent_today + estimated > cap:
        raise DailyLimitExceeded(
            f"order estimate ${estimated:.2f} + today's executed "
            f"${spent_today:.2f} would exceed daily cap ${cap:.2f}"
        )
