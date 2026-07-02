from __future__ import annotations

import json
from pathlib import Path

from signals.nasdaq import parse_splits_payload

FIXTURE = Path(__file__).parent / "fixtures" / "nasdaq_splits_sample.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def test_parse_returns_only_reverse_splits():
    signals = parse_splits_payload(load_fixture())
    assert signals, "fixture must contain at least one reverse split"
    for s in signals:
        num, den = s.ratio.split(":")
        assert int(num) < int(den), f"{s.ticker} ratio {s.ratio} is not a reverse split"


def test_ratio_is_normalized_no_spaces():
    signals = parse_splits_payload(load_fixture())
    for s in signals:
        assert " " not in s.ratio


def test_effective_date_is_iso_or_none():
    signals = parse_splits_payload(load_fixture())
    for s in signals:
        if s.effective_date is not None:
            assert len(s.effective_date) == 10 and s.effective_date[4] == "-"


def test_malformed_rows_are_skipped_not_fatal():
    payload = {"data": {"rows": [
        {"symbol": "GOOD", "ratio": "1 : 10", "executionDate": "7/14/2026"},
        {"symbol": "BADRATIO", "ratio": "n/a", "executionDate": "7/14/2026"},
        {"symbol": "PERCENT", "ratio": "5%", "executionDate": "7/14/2026"},
        {"ratio": "1 : 10", "executionDate": "7/14/2026"},  # no symbol
    ]}}
    signals = parse_splits_payload(payload)
    assert [s.ticker for s in signals] == ["GOOD"]


def test_missing_execution_date_yields_none():
    signals = parse_splits_payload(load_fixture())
    hand_edited = [s for s in signals if s.ticker == "UVIXNODATE"]
    assert hand_edited, "fixture must contain the hand-edited row with blank date"
    assert hand_edited[0].effective_date is None


def test_forward_and_decimal_splits_excluded():
    payload = {"data": {"rows": [
        {"symbol": "FWD", "ratio": "3 : 1", "executionDate": "7/21/2026"},
        {"symbol": "DEC", "ratio": "1.5:1", "executionDate": "7/20/2026"},
        {"symbol": "REV", "ratio": "1 : 10", "executionDate": "7/15/2026"},
    ]}}
    signals = parse_splits_payload(payload)
    assert [s.ticker for s in signals] == ["REV"]


def test_empty_payload_returns_empty_list():
    assert parse_splits_payload({"data": None}) == []
    assert parse_splits_payload({}) == []
    assert parse_splits_payload(None) == []
