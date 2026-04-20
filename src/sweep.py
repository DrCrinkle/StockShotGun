from __future__ import annotations

import asyncio
import math
import re
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class HoldingsOutcome(StrEnum):
    SUCCESS = "success"
    NO_CREDENTIALS = "no_creds"
    AUTH_FAILURE = "auth_fail"
    QUERY_ERROR = "query_error"


class SweepStatus(StrEnum):
    AWAITING_SPLIT = "awaiting_split"
    PROCESSING = "processing"
    FRACTIONAL_PENDING = "fractional_pending"
    SHARE_ARRIVED = "share_arrived"
    AMBIGUOUS = "ambiguous"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass(frozen=True)
class BrokerSplitProfile:
    clearing: str
    processing_window_days: int
    fractional_intermediate: bool
    round_up_expected: bool
    trade_may_be_blocked: bool
    cil_likely: bool
    notes: str


BROKER_PROFILES: dict[str, BrokerSplitProfile] = {
    "Robinhood": BrokerSplitProfile(
        "self",
        15,
        True,
        True,
        True,
        False,
        "Delivers fractional first, rounds up later",
    ),
    "TastyTrade": BrokerSplitProfile(
        "self",
        15,
        True,
        True,
        False,
        False,
        "Fractional may be permanently unsellable",
    ),
    "BBAE": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "$0.25 round-up fee"
    ),
    "DSPAC": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "Top RSA broker"
    ),
    "Firstrade": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "Follows issuer instructions"
    ),
    "Public": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "Reverse split fee applies"
    ),
    "SoFi": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "Shows PRESPLIT activity"
    ),
    "Webull": BrokerSplitProfile(
        "apex", 25, False, True, False, False, "Omnibus with Apex, not self-clearing"
    ),
    "Schwab": BrokerSplitProfile(
        "self", 5, False, False, False, True, "CIL only - does not round up"
    ),
    "WellsFargo": BrokerSplitProfile(
        "self", 10, False, True, False, False, "Cannot buy OTC under $1"
    ),
    "Chase": BrokerSplitProfile(
        "self", 10, False, False, False, True, "$5 OTC restriction, low RSA priority"
    ),
    "Fennel": BrokerSplitProfile(
        "unknown",
        20,
        False,
        True,
        True,
        False,
        "Blocks trading until share arrives",
    ),
    "Tradier": BrokerSplitProfile(
        "rqd", 20, False, False, False, False, "$0.75 reorg fee"
    ),
}

UNKNOWN_PROFILE = BrokerSplitProfile(
    "unknown", 20, False, False, False, False, "No split profile configured"
)


@dataclass
class SweepResult:
    broker: str
    account_id: str
    holdings_outcome: HoldingsOutcome
    status: SweepStatus
    observed_qty: float | None
    expected_post_qty: int
    pre_split_qty: int
    profile: BrokerSplitProfile
    details: str


HoldingsFn = Callable[[str], Awaitable[Any]]


def parse_ratio(ratio_str: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d+:\d+", ratio_str.strip()):
        raise ValueError("ratio must use N:D format, for example 1:25")

    numerator_text, denominator_text = ratio_str.split(":", 1)
    numerator = int(numerator_text)
    denominator = int(denominator_text)
    if numerator <= 0 or denominator <= 0:
        raise ValueError("ratio numerator and denominator must be positive")
    return numerator, denominator


def calculate_expected_post_qty(
    pre_split_qty: int, ratio_num: int, ratio_denom: int
) -> int:
    if pre_split_qty < 0:
        raise ValueError("pre-split quantity cannot be negative")
    return math.ceil(pre_split_qty * ratio_num / ratio_denom)


def classify_holding(
    observed_qty: float | None, pre_split_qty: int, expected_post_qty: int
) -> SweepStatus:
    if observed_qty is None or observed_qty == 0:
        return SweepStatus.PROCESSING
    if observed_qty == pre_split_qty and pre_split_qty == expected_post_qty:
        return SweepStatus.AMBIGUOUS
    if observed_qty == pre_split_qty and pre_split_qty != expected_post_qty:
        return SweepStatus.AWAITING_SPLIT
    if 0 < observed_qty < 1:
        return SweepStatus.FRACTIONAL_PENDING
    if (
        observed_qty >= 1
        and observed_qty <= expected_post_qty
        and observed_qty < pre_split_qty
    ):
        return SweepStatus.SHARE_ARRIVED
    return SweepStatus.PROCESSING


def status_summary(results: list[SweepResult]) -> dict[str, int]:
    summary = {
        "total_brokers_checked": len({result.broker for result in results}),
        "share_arrived": 0,
        "processing": 0,
        "fractional_pending": 0,
        "awaiting_split": 0,
        "ambiguous": 0,
        "skipped": 0,
        "error": 0,
    }
    for result in results:
        key = result.status.value
        if key in summary:
            summary[key] += 1
    return summary


async def sweep_broker(
    broker_name: str,
    ticker: str,
    holdings_fn: HoldingsFn,
    pre_split_qty: int,
    ratio_str: str,
) -> list[SweepResult]:
    ratio_num, ratio_denom = parse_ratio(ratio_str)
    expected_post_qty = calculate_expected_post_qty(
        pre_split_qty, ratio_num, ratio_denom
    )
    profile = BROKER_PROFILES.get(broker_name, UNKNOWN_PROFILE)

    try:
        holdings = await holdings_fn(ticker)
    except Exception as exc:
        return [
            _query_error_result(
                broker_name,
                exc,
                pre_split_qty,
                expected_post_qty,
                profile,
            )
        ]

    if holdings is None:
        return [
            SweepResult(
                broker=broker_name,
                account_id="---",
                holdings_outcome=HoldingsOutcome.NO_CREDENTIALS,
                status=SweepStatus.SKIPPED,
                observed_qty=None,
                expected_post_qty=expected_post_qty,
                pre_split_qty=pre_split_qty,
                profile=profile,
                details="No credentials configured or no broker session available",
            )
        ]

    normalized = _normalize_holdings(holdings)
    found_primary_positions = _has_positions(normalized)
    found_with_suffix = False

    if not found_primary_positions:
        suffix_ticker = f"{ticker}D"
        try:
            suffix_holdings = await holdings_fn(suffix_ticker)
        except Exception as exc:
            details = f"No primary position found; {suffix_ticker} query failed: {exc}"
            return _build_success_results(
                broker_name,
                normalized,
                pre_split_qty,
                expected_post_qty,
                profile,
                details,
            )

        if suffix_holdings is not None:
            suffix_normalized = _normalize_holdings(suffix_holdings)
            if _has_positions(suffix_normalized):
                normalized = suffix_normalized
                found_with_suffix = True

    details_prefix = (
        f"Found under temporary symbol {ticker}D; " if found_with_suffix else ""
    )
    return _build_success_results(
        broker_name,
        normalized,
        pre_split_qty,
        expected_post_qty,
        profile,
        details_prefix,
    )


async def sweep_all_brokers(
    ticker: str,
    ratio_str: str,
    pre_split_qty: int,
    broker_holdings: dict[str, HoldingsFn],
    selected_brokers: list[str] | None = None,
) -> list[SweepResult]:
    broker_names = selected_brokers or list(broker_holdings)
    tasks = [
        sweep_broker(
            broker_name,
            ticker,
            broker_holdings[broker_name],
            pre_split_qty,
            ratio_str,
        )
        for broker_name in broker_names
        if broker_name in broker_holdings
    ]
    gathered = await asyncio.gather(*tasks, return_exceptions=True)

    results: list[SweepResult] = []
    for broker_name, item in zip(broker_names, gathered, strict=False):
        profile = BROKER_PROFILES.get(broker_name, UNKNOWN_PROFILE)
        if isinstance(item, BaseException):
            expected = calculate_expected_post_qty(pre_split_qty, *parse_ratio(ratio_str))
            results.append(
                _query_error_result(broker_name, item, pre_split_qty, expected, profile)
            )
            continue
        results.extend(item)
    return results


def _normalize_holdings(holdings: Any) -> dict[str, list[dict[str, Any]]]:
    if isinstance(holdings, list):
        return {"default": [pos for pos in holdings if isinstance(pos, dict)]}
    if not isinstance(holdings, dict):
        return {}

    normalized: dict[str, list[dict[str, Any]]] = {}
    for account_id, positions in holdings.items():
        if isinstance(positions, list):
            normalized[str(account_id)] = [
                position for position in positions if isinstance(position, dict)
            ]
        elif isinstance(positions, dict):
            normalized[str(account_id)] = [positions]
        else:
            normalized[str(account_id)] = []
    return normalized


def _has_positions(holdings: dict[str, list[dict[str, Any]]]) -> bool:
    return any(bool(positions) for positions in holdings.values())


def _build_success_results(
    broker_name: str,
    holdings: dict[str, list[dict[str, Any]]],
    pre_split_qty: int,
    expected_post_qty: int,
    profile: BrokerSplitProfile,
    details_prefix: str,
) -> list[SweepResult]:
    if not holdings:
        holdings = {"---": []}

    results = []
    for account_id, positions in holdings.items():
        observed_qty = _sum_position_quantity(positions)
        status = classify_holding(observed_qty, pre_split_qty, expected_post_qty)
        results.append(
            SweepResult(
                broker=broker_name,
                account_id=account_id,
                holdings_outcome=HoldingsOutcome.SUCCESS,
                status=status,
                observed_qty=observed_qty,
                expected_post_qty=expected_post_qty,
                pre_split_qty=pre_split_qty,
                profile=profile,
                details=f"{details_prefix}{_details_for_status(status, profile)}",
            )
        )
    return results


def _sum_position_quantity(positions: list[dict[str, Any]]) -> float:
    total = 0.0
    for position in positions:
        try:
            total += float(position.get("quantity", 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _details_for_status(status: SweepStatus, profile: BrokerSplitProfile) -> str:
    if status == SweepStatus.SHARE_ARRIVED:
        if profile.trade_may_be_blocked:
            return "Share arrived; broker may block trading until processing completes"
        return "Share arrived; ready to sell manually"
    if status == SweepStatus.FRACTIONAL_PENDING:
        return "Fractional share delivered; waiting for round-up"
    if status == SweepStatus.AWAITING_SPLIT:
        return "Pre-split quantity still visible"
    if status == SweepStatus.AMBIGUOUS:
        return "Cannot distinguish pre-split from post-split quantity; use --force"
    if status == SweepStatus.PROCESSING:
        if profile.clearing == "apex":
            return "Processing; Apex-cleared brokers may take 3+ weeks"
        if profile.cil_likely:
            return "Processing; broker may pay cash-in-lieu"
        return "Processing; post-split share not visible yet"
    return profile.notes


def _query_error_result(
    broker_name: str,
    exc: BaseException,
    pre_split_qty: int,
    expected_post_qty: int,
    profile: BrokerSplitProfile,
) -> SweepResult:
    message = str(exc)
    lower_message = message.lower()
    auth_markers = ("auth", "login", "credential", "unauthorized", "forbidden")
    outcome = (
        HoldingsOutcome.AUTH_FAILURE
        if any(marker in lower_message for marker in auth_markers)
        else HoldingsOutcome.QUERY_ERROR
    )
    return SweepResult(
        broker=broker_name,
        account_id="---",
        holdings_outcome=outcome,
        status=SweepStatus.ERROR,
        observed_qty=None,
        expected_post_qty=expected_post_qty,
        pre_split_qty=pre_split_qty,
        profile=profile,
        details=f"{message}\n{traceback.format_exc()}",
    )
