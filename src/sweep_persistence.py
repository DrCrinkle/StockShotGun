from __future__ import annotations

from typing import Any

from rsa_store import RsaStore
from sweep import SweepResult


def load_trade_for_sweep(store: RsaStore, trade_id: int) -> dict[str, Any]:
    trade_row = store.get_trade(trade_id)
    if trade_row is None:
        raise LookupError(f"Trade {trade_id} not found")

    position_rows = store.get_raw_positions(trade_id)
    if not position_rows:
        raise LookupError(f"Trade {trade_id} has no positions")

    pre_qty_values = {row["pre_split_qty"] for row in position_rows}
    if len(pre_qty_values) > 1:
        raise ValueError(
            f"Trade {trade_id} has heterogeneous pre_split_qty across positions "
            f"({sorted(pre_qty_values)}); slice 1 requires homogeneous pre_qty"
        )

    return {
        "ticker": trade_row["ticker"],
        "split_ratio": trade_row["split_ratio"],
        "expected_split_date": trade_row["expected_split_date"],
        "pre_split_qty": next(iter(pre_qty_values)),
        "positions": [
            {
                "position_id": row["id"],
                "broker": row["broker"],
                "account_id": row["account_id"],
                "pre_split_qty": row["pre_split_qty"],
            }
            for row in position_rows
        ],
    }


def persist_sweep_results(
    store: RsaStore,
    position_lookup: dict[tuple[str, str], int],
    results: list[SweepResult],
    observed_at: str,
) -> None:
    for result in results:
        position_id = position_lookup.get((result.broker, result.account_id))
        if position_id is None:
            continue
        store.record_sweep(
            position_id=position_id,
            status=result.status,
            observed_qty=result.observed_qty,
            expected_post_qty=result.expected_post_qty,
            observed_at=observed_at,
            details=result.details or None,
        )
