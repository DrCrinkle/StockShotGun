from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from brokers.webull import _check_result_allows_order, _webull_place_result  # noqa: E402


class WebullCheckResultAllowsOrderTest(unittest.TestCase):
    """An order may be PLACED only when the pre-trade check explicitly cleared it.

    Regression guard: an empty check body used to become ``{}``, which slipped
    through as "allowed" and authorized an unvetted order.
    """

    def test_empty_check_result_is_not_allowed(self):
        allowed, _ = _check_result_allows_order({})
        self.assertFalse(allowed)

    def test_non_dict_is_not_allowed(self):
        allowed, _ = _check_result_allows_order(None)
        self.assertFalse(allowed)

    def test_explicit_block_is_not_allowed(self):
        allowed, reason = _check_result_allows_order(
            {"forward": False, "checkResultList": [{"code": "X", "msg": "blocked"}]}
        )
        self.assertFalse(allowed)
        self.assertIn("blocked", reason)

    def test_cleared_check_is_allowed(self):
        allowed, _ = _check_result_allows_order({"forward": True})
        self.assertTrue(allowed)


class WebullPlaceResultTest(unittest.TestCase):
    """A Webull placement counts as success only when the broker returned a
    confirming JSON payload — never on an empty or unparseable body.
    """

    def test_empty_body_is_not_success(self):
        result = _webull_place_result(200, b"", None)
        self.assertFalse(result["success"])

    def test_unparseable_body_is_not_success(self):
        result = _webull_place_result(200, b"<html>oops</html>", None)
        self.assertFalse(result["success"])

    def test_confirming_payload_is_returned_verbatim(self):
        payload = {"success": True, "orderId": "abc123"}
        self.assertEqual(_webull_place_result(200, b"{...}", payload), payload)

    def test_non_dict_payload_is_not_success(self):
        result = _webull_place_result(200, b"[]", [])
        self.assertFalse(result["success"])


if __name__ == "__main__":
    unittest.main()
