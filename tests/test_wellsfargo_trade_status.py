from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brokers.wellsfargo import (  # noqa: E402
    _wellsfargo_parse_price,
    _wellsfargo_trade_succeeded,
)


class WellsFargoTradeSucceededTest(unittest.TestCase):
    """Post-submit success detection must not book a rejected/unfilled order as sold.

    Regression guards: the old detector treated the generic phrases
    ``"order status"`` / ``"order detail"`` and the bare ``orderstatus`` /
    ``orderdetail`` URL markers as success — but a *rejected* order sits on
    exactly those pages, so it was booked as a completed sell.
    """

    def test_clear_confirmation_text_is_success(self):
        self.assertTrue(
            _wellsfargo_trade_succeeded("Your order has been placed. Order number 12345", "")
        )

    def test_confirmation_url_is_success(self):
        self.assertTrue(_wellsfargo_trade_succeeded("", "https://wf.example/trade/confirmation"))

    def test_rejected_order_on_status_page_is_not_success(self):
        self.assertFalse(
            _wellsfargo_trade_succeeded(
                "Order status: REJECTED. Order number 999",
                "https://wf.example/trade/orderstatus",
            )
        )

    def test_generic_order_status_text_alone_is_not_success(self):
        self.assertFalse(_wellsfargo_trade_succeeded("Order status", ""))

    def test_bare_orderstatus_url_alone_is_not_success(self):
        self.assertFalse(_wellsfargo_trade_succeeded("", "https://wf.example/trade/orderstatus"))

    def test_reject_phrase_overrides_confirmation_phrase(self):
        self.assertFalse(
            _wellsfargo_trade_succeeded("order accepted but ticker is not eligible", "")
        )


class WellsFargoParsePriceTest(unittest.TestCase):
    """Price parsing must return None (→ caller skips the account) rather than
    fabricate a price. The old code defaulted an unreadable price to $1.00,
    which forced a limit sell near $0.99 that never fills — a placed-but-unsold
    order the sweep then treats as sold.
    """

    def test_valid_price_string(self):
        self.assertEqual(_wellsfargo_parse_price("12.34"), 12.34)

    def test_empty_string_is_none(self):
        self.assertIsNone(_wellsfargo_parse_price(""))

    def test_whitespace_is_none(self):
        self.assertIsNone(_wellsfargo_parse_price("   "))

    def test_none_is_none(self):
        self.assertIsNone(_wellsfargo_parse_price(None))

    def test_non_numeric_is_none(self):
        self.assertIsNone(_wellsfargo_parse_price("N/A"))

    def test_non_positive_is_none(self):
        self.assertIsNone(_wellsfargo_parse_price("0"))


if __name__ == "__main__":
    unittest.main()
