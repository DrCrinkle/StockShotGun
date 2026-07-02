from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brokers.sofi import _sofi_order_succeeded  # noqa: E402


class SofiOrderSucceededTest(unittest.TestCase):
    def test_current_success_wording_with_order_id(self):
        # Real response observed 2026-06-01 — was being logged as a failure.
        result = {
            "header": "Your order has been placed",
            "subHeader": "You can review and cancel it anytime in your Activity.",
            "orderId": 225194154,
        }
        self.assertTrue(_sofi_order_succeeded(result))

    def test_legacy_success_wording(self):
        self.assertTrue(_sofi_order_succeeded({"header": "Your order is placed."}))

    def test_order_id_alone_is_success(self):
        self.assertTrue(_sofi_order_succeeded({"orderId": 1}))

    def test_genuine_failure_without_order_id_or_placed_header(self):
        self.assertFalse(
            _sofi_order_succeeded({"header": "Symbol cannot be traded", "orderId": 0})
        )

    def test_empty_and_non_dict_responses(self):
        self.assertFalse(_sofi_order_succeeded({}))
        self.assertFalse(_sofi_order_succeeded(None))
        self.assertFalse(_sofi_order_succeeded("error string"))


if __name__ == "__main__":
    unittest.main()
