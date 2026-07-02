from datetime import date, datetime

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


def test_promote_creates_pending_buy_signal(store):
    store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=NOW)
    row = store.list_calendar_signals()[0]
    buy_id = store.promote_calendar_signal(row["id"], now=NOW)

    buy = store.conn.execute(
        "SELECT * FROM buy_signals WHERE id = ?", (buy_id,)
    ).fetchone()
    assert buy["ticker"] == "ABCD"
    assert buy["ratio"] == "1:25"
    assert buy["target_date"] == "07/14"  # buy_signals uses MM/DD like recap parsing
    assert buy["status"] == "pending"

    updated = store.list_calendar_signals()[0]
    assert updated["status"] == "promoted"
    assert updated["promoted_buy_signal_id"] == buy_id


def test_promote_is_rejected_for_non_new_signal(store):
    store.upsert_calendar_signals([_signal()], source="nasdaq_calendar", now=NOW)
    row_id = store.list_calendar_signals()[0]["id"]
    store.dismiss_calendar_signal(row_id, reason="skip", now=NOW)
    with pytest.raises(ValueError):
        store.promote_calendar_signal(row_id, now=NOW)


def test_expire_stale_marks_past_effective_dates(store):
    store.upsert_calendar_signals(
        [_signal("OLDX", effective_date="2026-06-20"),
         _signal("NEWX", effective_date="2026-07-14")],
        source="nasdaq_calendar", now=NOW,
    )
    expired = store.expire_stale_calendar_signals(today=date(2026, 7, 1), now=NOW)
    assert expired == 1
    statuses = {r["ticker"]: r["status"] for r in store.list_calendar_signals()}
    assert statuses == {"OLDX": "expired", "NEWX": "new"}
