from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brokers.chase import _chase_order_filled  # noqa: E402


class ChaseOrderFilledTest(unittest.TestCase):
    """A Chase order counts as a completed trade ONLY when it actually filled.

    Regression guard: previously ``_trade_impl`` incremented ``success_count``
    for any terminal-ish status, so a CANCELLED / OPEN / UNKNOWN order was
    reported as a successful sell — which makes the downstream sweep believe
    shares were sold when they were not.
    """

    def test_fully_executed_is_filled(self):
        self.assertTrue(_chase_order_filled("FULLY_EXECUTED"))

    def test_partially_executed_is_filled(self):
        self.assertTrue(_chase_order_filled("PARTIALLY_EXECUTED"))

    def test_cancelled_is_not_filled(self):
        self.assertFalse(_chase_order_filled("CANCELLED"))

    def test_open_unfilled_is_not_filled(self):
        self.assertFalse(_chase_order_filled("OPEN"))

    def test_unknown_status_is_not_filled(self):
        self.assertFalse(_chase_order_filled("UNKNOWN"))

    def test_none_status_is_not_filled(self):
        self.assertFalse(_chase_order_filled(None))


if __name__ == "__main__":
    unittest.main()
