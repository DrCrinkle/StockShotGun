from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from rsa_store import RsaStore  # noqa: E402
from sweep import (  # noqa: E402
    BROKER_PROFILES,
    HoldingsOutcome,
    SweepResult,
    SweepStatus,
)
from sweep_persistence import load_trade_for_sweep, persist_sweep_results  # noqa: E402


def _make_result(
    broker: str,
    account_id: str,
    status: SweepStatus,
    observed_qty: float | None,
) -> SweepResult:
    return SweepResult(
        broker=broker,
        account_id=account_id,
        holdings_outcome=HoldingsOutcome.SUCCESS,
        status=status,
        observed_qty=observed_qty,
        expected_post_qty=1,
        pre_split_qty=3,
        profile=BROKER_PROFILES[broker],
        details="",
    )


class LoadTradeForSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RsaStore(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_returns_trade_fields_and_positions(self) -> None:
        trade_id = self.store.create_trade(
            "AREB", "1:25", expected_split_date="2026-05-15"
        )
        position_id = self.store.add_position(trade_id, "Fennel", "ACCT-1", 3)

        loaded = load_trade_for_sweep(self.store, trade_id)

        self.assertEqual(loaded["ticker"], "AREB")
        self.assertEqual(loaded["split_ratio"], "1:25")
        self.assertEqual(loaded["expected_split_date"], "2026-05-15")
        self.assertEqual(loaded["pre_split_qty"], 3)
        self.assertEqual(len(loaded["positions"]), 1)
        self.assertEqual(loaded["positions"][0]["broker"], "Fennel")
        self.assertEqual(loaded["positions"][0]["account_id"], "ACCT-1")
        self.assertEqual(loaded["positions"][0]["pre_split_qty"], 3)
        self.assertEqual(loaded["positions"][0]["position_id"], position_id)

    def test_raises_on_missing_trade(self) -> None:
        with self.assertRaises(LookupError):
            load_trade_for_sweep(self.store, 9999)

    def test_raises_on_no_positions(self) -> None:
        trade_id = self.store.create_trade("AREB", "1:25")
        with self.assertRaises(LookupError):
            load_trade_for_sweep(self.store, trade_id)

    def test_raises_on_heterogeneous_pre_qty(self) -> None:
        trade_id = self.store.create_trade("AREB", "1:25")
        self.store.add_position(trade_id, "Fennel", "ACCT-1", 3)
        self.store.add_position(trade_id, "Public", "ACCT-2", 5)
        with self.assertRaises(ValueError):
            load_trade_for_sweep(self.store, trade_id)

    def test_returns_multiple_positions_when_homogeneous(self) -> None:
        trade_id = self.store.create_trade("AREB", "1:25")
        self.store.add_position(trade_id, "Fennel", "ACCT-1", 3)
        self.store.add_position(trade_id, "Public", "ACCT-2", 3)
        loaded = load_trade_for_sweep(self.store, trade_id)
        self.assertEqual(len(loaded["positions"]), 2)
        self.assertEqual(loaded["pre_split_qty"], 3)


class PersistSweepResultsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RsaStore(":memory:")
        self.trade_id = self.store.create_trade("AREB", "1:25")
        self.fennel_pos = self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 3)
        self.public_pos = self.store.add_position(self.trade_id, "Public", "ACCT-2", 3)
        self.lookup = {
            ("Fennel", "ACCT-1"): self.fennel_pos,
            ("Public", "ACCT-2"): self.public_pos,
        }

    def tearDown(self) -> None:
        self.store.close()

    def test_writes_history_row_per_matching_result(self) -> None:
        results = [
            _make_result("Fennel", "ACCT-1", SweepStatus.SHARE_ARRIVED, 1.0),
            _make_result("Public", "ACCT-2", SweepStatus.PROCESSING, 0.0),
        ]
        persist_sweep_results(self.store, self.lookup, results, "2026-05-20T09:00:00")
        rows = self.store.conn.execute(
            "SELECT position_id, status FROM sweep_history ORDER BY position_id"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        statuses = {r["position_id"]: r["status"] for r in rows}
        self.assertEqual(statuses[self.fennel_pos], "share_arrived")
        self.assertEqual(statuses[self.public_pos], "processing")

    def test_upserts_state_row_per_matching_result(self) -> None:
        results = [_make_result("Fennel", "ACCT-1", SweepStatus.SHARE_ARRIVED, 1.0)]
        persist_sweep_results(self.store, self.lookup, results, "2026-05-20T09:00:00")
        row = self.store.conn.execute(
            "SELECT status, first_checked, last_checked FROM sweep_state WHERE position_id = ?",
            (self.fennel_pos,),
        ).fetchone()
        self.assertEqual(row["status"], "share_arrived")
        self.assertEqual(row["first_checked"], "2026-05-20T09:00:00")
        self.assertEqual(row["last_checked"], "2026-05-20T09:00:00")

    def test_skips_results_without_matching_position(self) -> None:
        results = [
            _make_result("Fennel", "ACCT-1", SweepStatus.SHARE_ARRIVED, 1.0),
            _make_result("Tradier", "MYSTERY", SweepStatus.PROCESSING, 0.0),
        ]
        persist_sweep_results(self.store, self.lookup, results, "2026-05-20T09:00:00")
        rows = self.store.conn.execute("SELECT position_id FROM sweep_history").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_id"], self.fennel_pos)

    def test_uses_single_observed_at_for_all_rows(self) -> None:
        results = [
            _make_result("Fennel", "ACCT-1", SweepStatus.SHARE_ARRIVED, 1.0),
            _make_result("Public", "ACCT-2", SweepStatus.PROCESSING, 0.0),
        ]
        persist_sweep_results(self.store, self.lookup, results, "2026-05-20T09:00:00")
        rows = self.store.conn.execute("SELECT observed_at FROM sweep_history").fetchall()
        self.assertEqual(len({r["observed_at"] for r in rows}), 1)


if __name__ == "__main__":
    unittest.main()
