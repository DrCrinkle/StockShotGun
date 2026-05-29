from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from rsa_store import RsaStore  # noqa: E402
from sweep import SweepStatus  # noqa: E402


class RsaStoreTradeTests(unittest.TestCase):
    def test_create_trade_returns_positive_id(self) -> None:
        store = RsaStore(":memory:")
        trade_id = store.create_trade("AREB", "1:25", expected_split_date="2026-05-15")
        self.assertIsInstance(trade_id, int)
        self.assertGreater(trade_id, 0)
        store.close()

    def test_create_trade_persists_all_fields(self) -> None:
        store = RsaStore(":memory:")
        trade_id = store.create_trade(
            "AREB",
            "1:25",
            expected_split_date="2026-05-15",
            signal_id=42,
            notes="Round 2",
        )
        row = store.conn.execute(
            "SELECT ticker, split_ratio, expected_split_date, signal_id, notes "
            "FROM rsa_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()
        self.assertEqual(row["ticker"], "AREB")
        self.assertEqual(row["split_ratio"], "1:25")
        self.assertEqual(row["expected_split_date"], "2026-05-15")
        self.assertEqual(row["signal_id"], 42)
        self.assertEqual(row["notes"], "Round 2")
        store.close()

    def test_init_schema_is_idempotent_on_existing_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "rsa.sqlite3")
            store_a = RsaStore(db_path)
            trade_id = store_a.create_trade("XYZ", "1:10")
            store_a.close()

            store_b = RsaStore(db_path)
            row = store_b.conn.execute(
                "SELECT ticker FROM rsa_trades WHERE id = ?", (trade_id,)
            ).fetchone()
            self.assertEqual(row["ticker"], "XYZ")
            store_b.close()


class RsaStorePositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RsaStore(":memory:")
        self.trade_id = self.store.create_trade("AREB", "1:25")

    def tearDown(self) -> None:
        self.store.close()

    def test_add_position_returns_positive_id(self) -> None:
        position_id = self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 3)
        self.assertIsInstance(position_id, int)
        self.assertGreater(position_id, 0)

    def test_add_position_persists_fields(self) -> None:
        position_id = self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 3)
        row = self.store.conn.execute(
            "SELECT trade_id, broker, account_id, pre_split_qty FROM rsa_positions WHERE id = ?",
            (position_id,),
        ).fetchone()
        self.assertEqual(row["trade_id"], self.trade_id)
        self.assertEqual(row["broker"], "Fennel")
        self.assertEqual(row["account_id"], "ACCT-1")
        self.assertEqual(row["pre_split_qty"], 3)

    def test_add_position_unique_constraint(self) -> None:
        self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 3)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 5)

    def test_add_position_none_account_dedupes(self) -> None:
        self.store.add_position(self.trade_id, "Tradier", None, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.add_position(self.trade_id, "Tradier", None, 2)


class RsaStoreSweepTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = RsaStore(":memory:")
        self.trade_id = self.store.create_trade("AREB", "1:25")
        self.position_id = self.store.add_position(self.trade_id, "Fennel", "ACCT-1", 3)

    def tearDown(self) -> None:
        self.store.close()

    def test_record_sweep_inserts_history_row(self) -> None:
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.PROCESSING,
            observed_qty=0.0,
            expected_post_qty=1,
            observed_at="2026-05-15T09:00:00",
            details="empty holdings",
        )
        rows = self.store.conn.execute(
            "SELECT status, observed_qty, expected_post_qty, observed_at, details "
            "FROM sweep_history WHERE position_id = ?",
            (self.position_id,),
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "processing")
        self.assertEqual(rows[0]["observed_qty"], 0.0)
        self.assertEqual(rows[0]["expected_post_qty"], 1)
        self.assertEqual(rows[0]["observed_at"], "2026-05-15T09:00:00")
        self.assertEqual(rows[0]["details"], "empty holdings")

    def test_record_sweep_inserts_state_row_on_first_call(self) -> None:
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.PROCESSING,
            observed_qty=0.0,
            expected_post_qty=1,
            observed_at="2026-05-15T09:00:00",
        )
        row = self.store.conn.execute(
            "SELECT status, first_checked, last_checked, sold_at "
            "FROM sweep_state WHERE position_id = ?",
            (self.position_id,),
        ).fetchone()
        self.assertEqual(row["status"], "processing")
        self.assertEqual(row["first_checked"], "2026-05-15T09:00:00")
        self.assertEqual(row["last_checked"], "2026-05-15T09:00:00")
        self.assertIsNone(row["sold_at"])

    def test_record_sweep_second_call_updates_state_and_appends_history(self) -> None:
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.PROCESSING,
            observed_qty=0.0,
            expected_post_qty=1,
            observed_at="2026-05-15T09:00:00",
        )
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.SHARE_ARRIVED,
            observed_qty=1.0,
            expected_post_qty=1,
            observed_at="2026-05-20T09:00:00",
        )
        state = self.store.conn.execute(
            "SELECT status, first_checked, last_checked FROM sweep_state WHERE position_id = ?",
            (self.position_id,),
        ).fetchone()
        self.assertEqual(state["status"], "share_arrived")
        self.assertEqual(state["first_checked"], "2026-05-15T09:00:00")
        self.assertEqual(state["last_checked"], "2026-05-20T09:00:00")

        history = self.store.conn.execute(
            "SELECT status, observed_at FROM sweep_history "
            "WHERE position_id = ? ORDER BY observed_at",
            (self.position_id,),
        ).fetchall()
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]["status"], "processing")
        self.assertEqual(history[1]["status"], "share_arrived")

    def test_mark_sold_sets_sold_at(self) -> None:
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.SHARE_ARRIVED,
            observed_qty=1.0,
            expected_post_qty=1,
            observed_at="2026-05-20T09:00:00",
        )
        self.store.mark_sold(self.position_id, "2026-05-21T10:30:00")
        row = self.store.conn.execute(
            "SELECT sold_at FROM sweep_state WHERE position_id = ?",
            (self.position_id,),
        ).fetchone()
        self.assertEqual(row["sold_at"], "2026-05-21T10:30:00")

    def test_list_positions_joins_state(self) -> None:
        other_position = self.store.add_position(self.trade_id, "Public", "ACCT-2", 4)
        self.store.record_sweep(
            self.position_id,
            status=SweepStatus.SHARE_ARRIVED,
            observed_qty=1.0,
            expected_post_qty=1,
            observed_at="2026-05-20T09:00:00",
        )
        rows = self.store.list_positions(self.trade_id)
        by_id = {r["id"]: r for r in rows}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id[self.position_id]["status"], "share_arrived")
        self.assertEqual(by_id[self.position_id]["broker"], "Fennel")
        self.assertIsNone(by_id[other_position]["status"])
        self.assertEqual(by_id[other_position]["broker"], "Public")


if __name__ == "__main__":
    unittest.main()
