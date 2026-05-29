from __future__ import annotations

import asyncio
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sweep import (  # noqa: E402
    BROKER_PROFILES,
    HoldingsOutcome,
    SweepStatus,
    calculate_expected_post_qty,
    classify_holding,
    parse_ratio,
    resolve_ambiguous_with_date,
    sweep_broker,
)


class SweepLogicTests(unittest.TestCase):
    def test_parse_ratio_accepts_valid_ratio(self) -> None:
        self.assertEqual(parse_ratio("1:25"), (1, 25))
        self.assertEqual(parse_ratio("3:1"), (3, 1))

    def test_parse_ratio_rejects_invalid_ratio(self) -> None:
        for value in ("abc", "1:", "", "0:25", "1:0"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_ratio(value)

    def test_calculate_expected_post_qty_rounds_up(self) -> None:
        self.assertEqual(calculate_expected_post_qty(1, 1, 25), 1)
        self.assertEqual(calculate_expected_post_qty(3, 1, 25), 1)
        self.assertEqual(calculate_expected_post_qty(51, 1, 25), 3)

    def test_classify_quantity_states(self) -> None:
        self.assertEqual(classify_holding(None, 1, 1), SweepStatus.PROCESSING)
        self.assertEqual(classify_holding(0, 1, 1), SweepStatus.PROCESSING)
        self.assertEqual(
            classify_holding(0.04, 1, 1), SweepStatus.FRACTIONAL_PENDING
        )
        self.assertEqual(classify_holding(3, 3, 1), SweepStatus.AWAITING_SPLIT)
        self.assertEqual(classify_holding(1, 1, 1), SweepStatus.AMBIGUOUS)
        self.assertEqual(classify_holding(1, 3, 1), SweepStatus.SHARE_ARRIVED)

    def test_all_project_brokers_have_profiles(self) -> None:
        expected_brokers = {
            "Robinhood",
            "Tradier",
            "TastyTrade",
            "Public",
            "Firstrade",
            "Fennel",
            "Schwab",
            "BBAE",
            "DSPAC",
            "SoFi",
            "Webull",
            "WellsFargo",
            "Chase",
        }
        self.assertEqual(set(BROKER_PROFILES), expected_brokers)


class SweepBrokerTests(unittest.TestCase):
    def test_sweep_broker_distinguishes_no_credentials(self) -> None:
        async def holdings_fn(_ticker: str):
            return None

        results = asyncio.run(
            sweep_broker("Tradier", "AREB", holdings_fn, 1, "1:25")
        )
        self.assertEqual(results[0].holdings_outcome, HoldingsOutcome.NO_CREDENTIALS)
        self.assertEqual(results[0].status, SweepStatus.SKIPPED)

    def test_sweep_broker_uses_d_suffix_fallback(self) -> None:
        async def holdings_fn(ticker: str):
            if ticker == "AREBD":
                return {"acct": [{"symbol": "AREBD", "quantity": 1}]}
            return {}

        results = asyncio.run(
            sweep_broker("Fennel", "AREB", holdings_fn, 3, "1:25")
        )
        self.assertEqual(results[0].status, SweepStatus.SHARE_ARRIVED)
        self.assertIn("AREBD", results[0].details)


class ResolveAmbiguousWithDateTests(unittest.TestCase):
    def test_passes_through_non_ambiguous_status(self) -> None:
        result = resolve_ambiguous_with_date(
            SweepStatus.PROCESSING,
            expected_split_date="2026-04-01",
            processing_window_days=10,
            today=date(2026, 5, 1),
        )
        self.assertEqual(result, SweepStatus.PROCESSING)

    def test_returns_ambiguous_when_no_expected_date(self) -> None:
        result = resolve_ambiguous_with_date(
            SweepStatus.AMBIGUOUS,
            expected_split_date=None,
            processing_window_days=10,
            today=date(2026, 5, 1),
        )
        self.assertEqual(result, SweepStatus.AMBIGUOUS)

    def test_resolves_to_share_arrived_after_window(self) -> None:
        result = resolve_ambiguous_with_date(
            SweepStatus.AMBIGUOUS,
            expected_split_date="2026-04-01",
            processing_window_days=10,
            today=date(2026, 4, 12),
        )
        self.assertEqual(result, SweepStatus.SHARE_ARRIVED)

    def test_stays_ambiguous_at_window_boundary(self) -> None:
        result = resolve_ambiguous_with_date(
            SweepStatus.AMBIGUOUS,
            expected_split_date="2026-04-01",
            processing_window_days=10,
            today=date(2026, 4, 11),
        )
        self.assertEqual(result, SweepStatus.AMBIGUOUS)

    def test_stays_ambiguous_inside_window(self) -> None:
        result = resolve_ambiguous_with_date(
            SweepStatus.AMBIGUOUS,
            expected_split_date="2026-04-01",
            processing_window_days=10,
            today=date(2026, 4, 5),
        )
        self.assertEqual(result, SweepStatus.AMBIGUOUS)


if __name__ == "__main__":
    unittest.main()
