from datetime import datetime

import pytest

from automation_recap import AutomationRecapStore
from signals.nasdaq import CalendarSignal

NOW = datetime(2026, 7, 1, 12, 0, 0)
LATER = datetime(2026, 7, 2, 12, 0, 0)


@pytest.fixture
def store(tmp_path):
    s = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    yield s
    s.conn.close()


def _signal(ticker="ABCD", ratio="1:25", effective_date="2026-07-14"):
    return CalendarSignal(
        ticker=ticker, ratio=ratio, effective_date=effective_date,
        company="ABCD Corp", raw={"symbol": ticker},
    )


def test_upsert_inserts_new_signal(store):
    counts = store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=NOW)
    assert counts == {"new": 1, "seen": 0}
    rows = store.list_calendar_signals(status="new")
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ABCD"
    assert rows[0]["ratio"] == "1:25"
    assert rows[0]["first_seen"] == NOW.isoformat()


def test_upsert_is_idempotent_updates_last_seen_only(store):
    store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=NOW)
    counts = store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=LATER)
    assert counts == {"new": 0, "seen": 1}
    rows = store.list_calendar_signals()
    assert len(rows) == 1
    assert rows[0]["first_seen"] == NOW.isoformat()
    assert rows[0]["last_seen"] == LATER.isoformat()


def test_upsert_does_not_resurrect_dismissed_signal(store):
    store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=NOW)
    row_id = store.list_calendar_signals()[0]["id"]
    store.dismiss_calendar_signal(row_id, reason="price too high", now=NOW)
    store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=LATER)
    rows = store.list_calendar_signals()
    assert len(rows) == 1
    assert rows[0]["status"] == "dismissed"
    assert rows[0]["dismissed_reason"] == "price too high"


def test_list_filters_by_status(store):
    store.upsert_calendar_signals(
        [_signal("AAAA"), _signal("BBBB")], source="nasdaq_calendar", now=NOW
    )
    a_id = [r["id"] for r in store.list_calendar_signals() if r["ticker"] == "AAAA"][0]
    store.dismiss_calendar_signal(a_id, reason="skip", now=NOW)
    assert [r["ticker"] for r in store.list_calendar_signals(status="new")] == ["BBBB"]
    assert [r["ticker"] for r in store.list_calendar_signals(status="dismissed")] == ["AAAA"]
