from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brokers.chase import _chase_order_filled, _chase_status_is_terminal  # noqa: E402


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


class ChaseStatusIsTerminalTest(unittest.TestCase):
    """Polling may stop only on a fill or a hard cancel. OPEN is NOT terminal —
    a freshly submitted order often passes through OPEN before it fills, so the
    poll must keep waiting rather than bail and report the leg unfilled (which
    would make the sweep retry and double-sell).
    """

    def test_open_is_not_terminal(self):
        self.assertFalse(_chase_status_is_terminal("OPEN"))

    def test_fully_executed_is_terminal(self):
        self.assertTrue(_chase_status_is_terminal("FULLY_EXECUTED"))

    def test_partially_executed_is_terminal(self):
        self.assertTrue(_chase_status_is_terminal("PARTIALLY_EXECUTED"))

    def test_cancelled_is_terminal(self):
        self.assertTrue(_chase_status_is_terminal("CANCELLED"))

    def test_unknown_and_none_are_not_terminal(self):
        self.assertFalse(_chase_status_is_terminal("UNKNOWN"))
        self.assertFalse(_chase_status_is_terminal(None))


if __name__ == "__main__":
    unittest.main()
