# RSA Signals Detection Implementation Plan (Plan 1 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automated reverse-split detection from the Nasdaq corporate-actions calendar, persisted into the existing signal store, exposed via CLI + MCP tools, plus a `status --json` command for the Pulse feed.

**Architecture:** A new `signals` package fetches and parses the Nasdaq splits calendar (deterministic, no LLM). Hits are upserted into a new `calendar_signals` table in the existing `AutomationRecapStore` (same SQLite file as everything else, `logs/automation.sqlite3`). Worthwhile signals are *promoted* into the existing `buy_signals` queue that the `automate` due-buy path already consumes. The `ExecutionEngine` gets `scan_signals` / `dismiss_signal` / `promote_signal` methods, wrapped as FastMCP tools on `ssg-router`. A `status` CLI command emits an aggregate JSON snapshot for Pulse polling.

**Tech Stack:** Python 3.13, httpx (already a dep), sqlite3, FastMCP (existing), pytest. Run tests with `.venv/bin/python -m pytest` — system python lacks `h2`.

**Scope note:** This is Plan 1 (repo-side) of the spec `specs/agent-operated-rsa-engine.md`. Plan 2 (PAI-side: `RsaTrader` skill, scheduled routine, notification/approval flow, Pulse module) is written after this plan ships, because the skill's instructions depend on the final MCP tool shapes.

---

## Execution deviations (as built)

This plan shipped with a few departures from the task bodies below. The task
steps are left as originally written (they're still an accurate record of the
TDD process); this section is the correction layer.

- **CLI envelope (Tasks 4 & 6).** `main.py` had already grown a shared handler
  contract by the time these tasks landed: `_run_x(args, parser, context) ->
  tuple[ExitCode, dict]`, with JSON-vs-text rendering decided centrally by a
  global `--output json` flag (not a per-command `--json` flag). `signals` and
  `status` were built to that contract instead of the plan's self-printing
  `--json` idiom. The plan's test snippets (`run_signals(args)`, bare
  `argparse.Namespace` with a `json` attribute) are superseded by
  `_run_signals(args, parser, context)` / `_run_status(args, parser, context)`
  — see `src/cli/signals.py` and `src/cli/status.py` for the actual shape.
- **Engine class name (Task 5).** The plan assumes an `ExecutionEngine` class;
  that rename hadn't landed on this branch, so the methods live on `Router`
  (`src/agentic/router/_server.py`) instead. `dismiss_calendar_signal` and
  `promote_calendar_signal` raise `ValueError` at the store layer, which the
  MCP-layer methods (`dismiss_signal`, `promote_signal`) translate into
  `{"ok": False, "error": ...}` rather than letting the exception propagate.
  `scan_signals` similarly catches calendar fetch/parse failures and returns a
  structured `{"ok": False, "error": ..., "source": ...}` instead of raising —
  a fetch failure is routine (upstream calendar flakiness), not fatal.
- **Extra hardening from review loops.** Golden fixture assertions on the
  captured Nasdaq payload; a dismiss state guard (only `'new'` signals can be
  dismissed or promoted, enforced with `ValueError` otherwise); a
  `CalendarSignalLike` Protocol so store methods don't hard-depend on the
  concrete `CalendarSignal` dataclass; explicit no-fetch sentinels in tests
  (fetchers that raise if called, to prove `refresh=False` never hits the
  network); and an honest `promote_signal` docstring — promoted signals enter
  the `automate` due-buy queue rather than executing immediately, and a
  signal with a NULL `effective_date` is immediately due on the next
  `automate` run.

---

## File structure

| File | Responsibility |
|---|---|
| `src/signals/__init__.py` | Package exports (`CalendarSignal`, `fetch_splits_calendar`, `parse_splits_payload`) |
| `src/signals/nasdaq.py` | Nasdaq splits-calendar fetch + parse. No storage, no CLI — pure source adapter |
| `src/automation_recap.py` | (modify) `calendar_signals` table + upsert/list/dismiss/promote/expire methods on `AutomationRecapStore` |
| `src/cli/signals.py` | `signals scan` / `signals list` CLI handler |
| `src/cli/status.py` | `status` CLI handler — aggregate JSON snapshot for Pulse |
| `src/main.py` | (modify) register `signals` and `status` actions |
| `src/agentic/router/_server.py` | (modify) engine methods + FastMCP tool wrappers |
| `tests/fixtures/nasdaq_splits_sample.json` | Captured real payload (sanitized) |
| `tests/test_signals_nasdaq.py` | Parser unit tests |
| `tests/test_calendar_signals_store.py` | Store method tests |
| `tests/test_cli_signals.py` | CLI handler tests |
| `tests/test_cli_status.py` | Status command tests |
| `tests/agentic/test_calendar_signals_router.py` | Engine method + MCP wiring tests |

---

### Task 1: Nasdaq source adapter (`signals` package)

**Files:**
- Create: `src/signals/__init__.py`, `src/signals/nasdaq.py`
- Create: `tests/fixtures/nasdaq_splits_sample.json`, `tests/test_signals_nasdaq.py`

- [ ] **Step 1: Capture a real payload into the fixture**

```bash
curl -s 'https://api.nasdaq.com/api/calendar/splits?date=2026-07-01' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36' \
  -H 'Accept: application/json' | python3 -m json.tool > tests/fixtures/nasdaq_splits_sample.json
```

Inspect the file. The expected shape is `{"data": {"rows": [{"symbol": ..., "companyName"/"name": ..., "ratio": "1 : 25", "executionDate": "07/14/2026", ...}]}}`. **If the real field names differ, adjust the parser code in Step 3 to match the fixture — the fixture is the source of truth, not this plan.** Ensure the fixture contains at least one reverse split (ratio `1 : N`), one forward split (ratio `N : 1`), and one row with a missing/blank date (hand-edit a copy of a real row if needed, marking it clearly).

- [ ] **Step 2: Write the failing parser tests**

```python
# tests/test_signals_nasdaq.py
import json
from pathlib import Path

from signals.nasdaq import CalendarSignal, parse_splits_payload

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
        {"symbol": "GOOD", "ratio": "1 : 10", "executionDate": "07/14/2026"},
        {"symbol": "BADRATIO", "ratio": "n/a", "executionDate": "07/14/2026"},
        {"ratio": "1 : 10", "executionDate": "07/14/2026"},  # no symbol
    ]}}
    signals = parse_splits_payload(payload)
    assert [s.ticker for s in signals] == ["GOOD"]


def test_empty_payload_returns_empty_list():
    assert parse_splits_payload({"data": None}) == []
    assert parse_splits_payload({}) == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_signals_nasdaq.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'signals'`

- [ ] **Step 4: Implement the source adapter**

```python
# src/signals/nasdaq.py
"""Nasdaq corporate-actions splits calendar source adapter.

Pure source layer: fetch + parse only. Storage lives in
`AutomationRecapStore.upsert_calendar_signals`; orchestration in the
`signals` CLI and `ExecutionEngine.scan_signals`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

NASDAQ_SPLITS_URL = "https://api.nasdaq.com/api/calendar/splits"

# Nasdaq's API returns empty/hangs without browser-like headers.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

_RATIO_RE = re.compile(r"^\s*(\d+)\s*[:x/]\s*(\d+)\s*$")

SOURCE_NAME = "nasdaq_calendar"


@dataclass(frozen=True)
class CalendarSignal:
    ticker: str
    ratio: str  # normalized "N:D", reverse splits only (N < D)
    effective_date: str | None  # ISO YYYY-MM-DD, None if unparseable
    company: str
    raw: dict[str, Any] = field(compare=False)


def _parse_ratio(raw_ratio: Any) -> tuple[int, int] | None:
    if not isinstance(raw_ratio, str):
        return None
    m = _RATIO_RE.match(raw_ratio)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_date(raw_date: Any) -> str | None:
    if not isinstance(raw_date, str) or not raw_date.strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw_date.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_splits_payload(payload: dict[str, Any]) -> list[CalendarSignal]:
    """Extract reverse splits from a Nasdaq splits-calendar payload.

    Malformed rows are skipped, never fatal — the calendar is upstream
    data we don't control.
    """
    data = payload.get("data") or {}
    rows = data.get("rows") or []
    signals: list[CalendarSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("symbol")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        parsed = _parse_ratio(row.get("ratio"))
        if parsed is None:
            continue
        num, den = parsed
        if num >= den:  # forward split or 1:1 — not an RSA candidate
            continue
        signals.append(
            CalendarSignal(
                ticker=ticker.strip().upper(),
                ratio=f"{num}:{den}",
                effective_date=_parse_date(
                    row.get("executionDate") or row.get("payableDate")
                ),
                company=str(row.get("companyName") or row.get("name") or ""),
                raw=row,
            )
        )
    return signals


async def fetch_splits_calendar(date_str: str | None = None) -> dict[str, Any]:
    """Fetch the raw splits-calendar payload for a date (defaults to today
    on the Nasdaq side). Network errors propagate to the caller."""
    params = {"date": date_str} if date_str else {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(NASDAQ_SPLITS_URL, params=params, headers=_HEADERS)
        response.raise_for_status()
        return response.json()
```

```python
# src/signals/__init__.py
from signals.nasdaq import (
    SOURCE_NAME,
    CalendarSignal,
    fetch_splits_calendar,
    parse_splits_payload,
)

__all__ = [
    "SOURCE_NAME",
    "CalendarSignal",
    "fetch_splits_calendar",
    "parse_splits_payload",
]
```

Note: `dataclass(frozen=True)` with a `dict` field — `raw` is excluded from comparison via `field(compare=False)`; do not add `__hash__` use on these objects.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_signals_nasdaq.py -v`
Expected: 5 PASS. If field-name mismatches with the real fixture surface here, fix the parser (Step 1 note), not the fixture.

- [ ] **Step 6: Commit**

```bash
git add src/signals tests/test_signals_nasdaq.py tests/fixtures/nasdaq_splits_sample.json
git commit -m "feat(signals): Nasdaq splits-calendar source adapter"
```

---

### Task 2: `calendar_signals` table + upsert/list/dismiss methods

**Files:**
- Modify: `src/automation_recap.py` (schema in `_init_schema` at ~line 299; new methods after `mark_buy_signals_executed` at ~line 570)
- Create: `tests/test_calendar_signals_store.py`

- [ ] **Step 1: Write the failing store tests**

```python
# tests/test_calendar_signals_store.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_calendar_signals_store.py -v`
Expected: FAIL with `AttributeError: 'AutomationRecapStore' object has no attribute 'upsert_calendar_signals'`

- [ ] **Step 3: Add the table to `_init_schema`**

Append inside the existing `executescript` string in `AutomationRecapStore._init_schema` (after the `tba_candidates` block, before the closing `"""`):

```sql
            CREATE TABLE IF NOT EXISTS calendar_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                ratio TEXT NOT NULL,
                effective_date TEXT,
                source TEXT NOT NULL,
                raw_json TEXT NOT NULL,
                signal_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                dismissed_reason TEXT,
                promoted_buy_signal_id INTEGER REFERENCES buy_signals(id),
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL
            );
```

Statuses: `new` → (`promoted` | `dismissed` | `expired`). Terminal states are never overwritten by upsert.

- [ ] **Step 4: Implement upsert/list/dismiss**

Add after `mark_buy_signals_executed` (~line 577), following the file's existing style (`now: datetime` injectable, `_line_hash` for keys, `sqlite3.Row` returns):

```python
    # --- calendar signals (automated source feed, e.g. Nasdaq calendar) ---

    def upsert_calendar_signals(
        self,
        signals: list[Any],
        source: str,
        now: datetime,
    ) -> dict[str, int]:
        """Idempotent ingest of CalendarSignal objects. New rows get status
        'new'; existing rows (any status) only bump last_seen — terminal
        states (dismissed/promoted/expired) are never resurrected."""
        now_iso = now.isoformat()
        new = 0
        seen = 0
        for signal in signals:
            key = _line_hash(
                f"{source}|{signal.ticker}|{signal.ratio}|{signal.effective_date}"
            )
            exists = self.conn.execute(
                "SELECT 1 FROM calendar_signals WHERE signal_key = ?", (key,)
            ).fetchone()
            if exists:
                self.conn.execute(
                    "UPDATE calendar_signals SET last_seen = ? WHERE signal_key = ?",
                    (now_iso, key),
                )
                seen += 1
            else:
                self.conn.execute(
                    """
                    INSERT INTO calendar_signals(
                        ticker, ratio, effective_date, source, raw_json,
                        signal_key, status, first_seen, last_seen
                    ) VALUES (?, ?, ?, ?, ?, ?, 'new', ?, ?)
                    """,
                    (
                        signal.ticker,
                        signal.ratio,
                        signal.effective_date,
                        source,
                        json.dumps(signal.raw),
                        key,
                        now_iso,
                        now_iso,
                    ),
                )
                new += 1
        self.conn.commit()
        return {"new": new, "seen": seen}

    def list_calendar_signals(self, status: str | None = None) -> list[sqlite3.Row]:
        if status is None:
            return self.conn.execute(
                "SELECT * FROM calendar_signals ORDER BY effective_date, ticker"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM calendar_signals WHERE status = ? "
            "ORDER BY effective_date, ticker",
            (status,),
        ).fetchall()

    def dismiss_calendar_signal(self, signal_id: int, reason: str, now: datetime) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE calendar_signals SET status = 'dismissed', "
                "dismissed_reason = ?, last_seen = ? WHERE id = ?",
                (reason, now.isoformat(), signal_id),
            )
```

`import json` is likely already present at the top of `automation_recap.py` (used for `brokers_json`) — verify, and add if not.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_calendar_signals_store.py -v`
Expected: 4 PASS

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass (schema change is additive; `CREATE TABLE IF NOT EXISTS` is safe on existing DBs).

- [ ] **Step 7: Commit**

```bash
git add src/automation_recap.py tests/test_calendar_signals_store.py
git commit -m "feat(signals): calendar_signals table with idempotent upsert"
```

---

### Task 3: promote + expire store methods

**Files:**
- Modify: `src/automation_recap.py`
- Modify: `tests/test_calendar_signals_store.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_calendar_signals_store.py`:

```python
from datetime import date


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_calendar_signals_store.py -v -k "promote or expire"`
Expected: FAIL with `AttributeError: ... 'promote_calendar_signal'`

- [ ] **Step 3: Implement promote + expire**

```python
    def promote_calendar_signal(self, signal_id: int, now: datetime) -> int:
        """Promote a 'new' calendar signal into the actionable buy_signals
        queue (consumed by the automate due-buy path). Returns buy_signal id."""
        row = self.conn.execute(
            "SELECT * FROM calendar_signals WHERE id = ?", (signal_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"calendar signal {signal_id} not found")
        if row["status"] != "new":
            raise ValueError(
                f"calendar signal {signal_id} is '{row['status']}', not 'new'"
            )

        target_mmdd = None
        if row["effective_date"]:
            iso = datetime.strptime(row["effective_date"], "%Y-%m-%d")
            target_mmdd = iso.strftime("%m/%d")

        key = _line_hash(f"calendar_promote|{row['signal_key']}")
        with self.conn:
            cursor = self.conn.execute(
                """
                INSERT INTO buy_signals(
                    ticker, target_date, ratio, round_num, notes,
                    signal_key, status, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?, 'pending', ?)
                """,
                (
                    row["ticker"],
                    target_mmdd,
                    row["ratio"],
                    f"promoted from calendar_signals #{signal_id} ({row['source']})",
                    key,
                    now.isoformat(),
                ),
            )
            buy_id = cursor.lastrowid
            assert buy_id is not None
            self.conn.execute(
                "UPDATE calendar_signals SET status = 'promoted', "
                "promoted_buy_signal_id = ?, last_seen = ? WHERE id = ?",
                (buy_id, now.isoformat(), signal_id),
            )
        return buy_id

    def expire_stale_calendar_signals(self, today: date, now: datetime) -> int:
        """Mark 'new' signals whose effective date has passed as expired."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE calendar_signals SET status = 'expired', last_seen = ? "
                "WHERE status = 'new' AND effective_date IS NOT NULL "
                "AND effective_date < ?",
                (now.isoformat(), today.isoformat()),
            )
        return cursor.rowcount
```

Add `from datetime import date` to the imports at the top of `automation_recap.py` (it already imports `datetime`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_calendar_signals_store.py -v`
Expected: 7 PASS

- [ ] **Step 5: Commit**

```bash
git add src/automation_recap.py tests/test_calendar_signals_store.py
git commit -m "feat(signals): promote-to-buy-queue and expiry for calendar signals"
```

---

### Task 4: `signals` CLI command

**Files:**
- Create: `src/cli/signals.py`
- Modify: `src/main.py` (`known_actions` at line 57, subparser registration near line 589, dispatch near line 279)
- Create: `tests/test_cli_signals.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_signals.py
import argparse
import asyncio
import json
from datetime import datetime

from automation_recap import AutomationRecapStore
from cli.signals import run_signals
from signals.nasdaq import CalendarSignal


def _args(tmp_path, action="scan", as_json=True, status=None):
    return argparse.Namespace(
        signals_action=action,
        db_path=str(tmp_path / "automation.sqlite3"),
        json=as_json,
        status=status,
    )


def _fake_fetcher(signals):
    async def fetch():
        return signals
    return fetch


def test_scan_persists_and_reports_counts(tmp_path, capsys):
    sig = CalendarSignal(
        ticker="ABCD", ratio="1:25", effective_date="2026-07-14",
        company="ABCD Corp", raw={},
    )
    exit_code = asyncio.run(
        run_signals(_args(tmp_path), fetcher=_fake_fetcher([sig]), now=datetime(2026, 7, 1))
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["counts"] == {"new": 1, "seen": 0, "expired": 0}
    assert out["signals"][0]["ticker"] == "ABCD"

    store = AutomationRecapStore(str(tmp_path / "automation.sqlite3"))
    assert len(store.list_calendar_signals(status="new")) == 1
    store.conn.close()


def test_scan_expires_stale_signals(tmp_path, capsys):
    stale = CalendarSignal(
        ticker="OLDX", ratio="1:10", effective_date="2026-06-01",
        company="Old Co", raw={},
    )
    asyncio.run(
        run_signals(_args(tmp_path), fetcher=_fake_fetcher([stale]), now=datetime(2026, 7, 1))
    )
    out = json.loads(capsys.readouterr().out)
    assert out["counts"]["expired"] == 1


def test_list_reads_without_network(tmp_path, capsys):
    sig = CalendarSignal(
        ticker="ABCD", ratio="1:25", effective_date="2026-07-14",
        company="ABCD Corp", raw={},
    )
    asyncio.run(
        run_signals(_args(tmp_path), fetcher=_fake_fetcher([sig]), now=datetime(2026, 7, 1))
    )
    capsys.readouterr()

    exit_code = asyncio.run(
        run_signals(_args(tmp_path, action="list"), fetcher=None, now=datetime(2026, 7, 1))
    )
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["signals"][0]["ticker"] == "ABCD"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_signals.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli.signals'`

- [ ] **Step 3: Implement the handler**

```python
# src/cli/signals.py
"""`signals` command handler: scan the Nasdaq splits calendar into the
calendar_signals table, or list what's staged."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import datetime

from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]
from signals.nasdaq import (  # type: ignore[import-untyped]
    SOURCE_NAME,
    CalendarSignal,
    fetch_splits_calendar,
    parse_splits_payload,
)


async def _default_fetcher() -> list[CalendarSignal]:
    payload = await fetch_splits_calendar()
    return parse_splits_payload(payload)


def _row_to_dict(row) -> dict:
    return {key: row[key] for key in row.keys()}


async def run_signals(
    args,
    fetcher: Callable[[], Awaitable[list[CalendarSignal]]] | None = None,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now()
    store = AutomationRecapStore(args.db_path)
    try:
        counts = {"new": 0, "seen": 0, "expired": 0}
        if args.signals_action == "scan":
            fetch = fetcher or _default_fetcher
            signals = await fetch()
            upserted = store.upsert_calendar_signals(signals, source=SOURCE_NAME, now=now)
            counts.update(upserted)
            counts["expired"] = store.expire_stale_calendar_signals(
                today=now.date(), now=now
            )

        status_filter = getattr(args, "status", None) or (
            "new" if args.signals_action == "scan" else None
        )
        rows = [_row_to_dict(r) for r in store.list_calendar_signals(status=status_filter)]

        if args.json:
            print(json.dumps({"ok": True, "counts": counts, "signals": rows}, indent=2))
        else:
            if args.signals_action == "scan":
                print(
                    f"Scan complete: {counts['new']} new, {counts['seen']} seen, "
                    f"{counts['expired']} expired"
                )
            if not rows:
                print("No calendar signals.")
            for row in rows:
                print(
                    f"  [{row['id']:>4}] {row['ticker']:<6} {row['ratio']:<8} "
                    f"effective {row['effective_date'] or 'unknown':<12} {row['status']}"
                )
        return 0
    finally:
        store.conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_signals.py -v`
Expected: 3 PASS

- [ ] **Step 5: Wire into `main.py`**

Three edits, following the exact pattern the `sweep` action uses:

1. Line 57 — add to the set: `known_actions = {"buy", "sell", "setup", "holdings", "health", "automate", "sweep", "signals", "status"}` (add `"status"` now too; Task 6 uses it).
2. After the `sweep_parser` block (~line 589) add:

```python
    signals_parser = subparsers.add_parser(
        "signals",
        help="Scan/list reverse-split signals from the Nasdaq calendar",
    )
    signals_parser.add_argument(
        "signals_action",
        choices=["scan", "list"],
        help="scan: poll the calendar and persist; list: read staged signals",
    )
    signals_parser.add_argument(
        "--status",
        default=None,
        help="Filter listed signals by status (new, promoted, dismissed, expired)",
    )
    signals_parser.add_argument("--json", action="store_true", help="JSON output")
    signals_parser.add_argument(
        "--db-path",
        default=db_default,
        help="SQLite path for automation state and dedupe",
    )
```

(`db_default` is the same variable the automate parser already uses at line ~527 — confirm its scope covers this insertion point; if the parsers are built in the same function it does.)

3. In the dispatch block (~line 279, next to `if args.action == "sweep":`):

```python
    if args.action == "signals":
        from cli.signals import run_signals

        return await run_signals(args)
```

- [ ] **Step 6: Smoke-test the wiring**

Run: `.venv/bin/python src/main.py signals list --json --db-path /tmp/ssg-signals-test.sqlite3`
Expected: `{"ok": true, "counts": {...}, "signals": []}` and exit 0.

Optionally run a real scan once to validate the live endpoint end-to-end:
`.venv/bin/python src/main.py signals scan --db-path /tmp/ssg-signals-test.sqlite3`

- [ ] **Step 7: Run full suite, then commit**

Run: `.venv/bin/python -m pytest tests/ -q` — expected all pass.

```bash
git add src/cli/signals.py src/main.py tests/test_cli_signals.py
git commit -m "feat(cli): signals scan/list command"
```

---

### Task 5: engine methods + MCP tools (`scan_signals`, `dismiss_signal`, `promote_signal`)

**Files:**
- Modify: `src/agentic/router/_server.py` (engine methods after `recap_ingest` ~line 470; FastMCP wrappers inside `build_router_fastmcp_server` after `recap_ingest` tool ~line 992)
- Create: `tests/agentic/test_calendar_signals_router.py`

- [ ] **Step 1: Write the failing tests**

Follow the construction pattern used by `tests/agentic/test_recap_ingest.py` (read it first; it shows how to build an `ExecutionEngine` with a temp `automation_store_path` and no real brokers). The tests below assume an `engine` fixture built that way — copy the fixture from `test_recap_ingest.py` and add `automation_store_path=str(tmp_path / "automation.sqlite3")`.

```python
# tests/agentic/test_calendar_signals_router.py
import asyncio
from datetime import datetime

import pytest

from automation_recap import AutomationRecapStore
from signals.nasdaq import CalendarSignal

# Reuse/adapt the engine fixture from test_recap_ingest.py:
# engine = ExecutionEngine(broker_servers={}, core=<test core>, provider=<test provider>,
#                          automation_store_path=str(tmp_path / "automation.sqlite3"))


def _seed_signal(db_path: str) -> int:
    store = AutomationRecapStore(db_path)
    store.upsert_calendar_signals(
        [CalendarSignal(ticker="ABCD", ratio="1:25",
                        effective_date="2099-01-15", company="", raw={})],
        source="nasdaq_calendar",
        now=datetime(2026, 7, 1),
    )
    row_id = store.list_calendar_signals()[0]["id"]
    store.conn.close()
    return row_id


def test_scan_signals_refresh_false_reads_store(engine):
    _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.scan_signals(refresh=False))
    assert result["ok"] is True
    assert result["signals"][0]["ticker"] == "ABCD"
    assert result["counts"] == {"new": 0, "seen": 0, "expired": 0}


def test_scan_signals_refresh_true_uses_injected_fetcher(engine):
    async def fake_fetch():
        return [CalendarSignal(ticker="ZZZZ", ratio="1:8",
                               effective_date="2099-02-01", company="", raw={})]

    engine.calendar_fetcher = fake_fetch
    result = asyncio.run(engine.scan_signals(refresh=True))
    assert result["counts"]["new"] == 1
    assert result["signals"][0]["ticker"] == "ZZZZ"


def test_dismiss_signal(engine):
    row_id = _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.dismiss_signal(signal_id=row_id, reason="illiquid"))
    assert result["ok"] is True
    store = AutomationRecapStore(engine.automation_store_path)
    assert store.list_calendar_signals()[0]["status"] == "dismissed"
    store.conn.close()


def test_promote_signal_creates_buy_signal(engine):
    row_id = _seed_signal(engine.automation_store_path)
    result = asyncio.run(engine.promote_signal(signal_id=row_id))
    assert result["ok"] is True
    assert isinstance(result["buy_signal_id"], int)


def test_promote_unknown_signal_errors_cleanly(engine):
    result = asyncio.run(engine.promote_signal(signal_id=99999))
    assert result["ok"] is False
    assert "not found" in result["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/agentic/test_calendar_signals_router.py -v`
Expected: FAIL with `AttributeError: 'ExecutionEngine' object has no attribute 'scan_signals'` (after the fixture is adapted).

- [ ] **Step 3: Add the dataclass field and engine methods**

Add to the `ExecutionEngine` dataclass fields (after `automation_store_path`, ~line 194):

```python
    # Injectable for tests; None = fetch from the real Nasdaq calendar.
    calendar_fetcher: Any = None
```

Add methods after `recap_ingest` (~line 470), matching its style (`@logged_tool`, lazy imports, try/finally store close):

```python
    @logged_tool(tool="router.scan_signals")
    async def scan_signals(self, refresh: bool = True) -> dict[str, Any]:
        """Scan the reverse-split calendar into calendar_signals (refresh=True)
        or just read staged 'new' signals (refresh=False). Read/ingest only —
        never proposes or executes orders."""
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        now = _dt.now()
        store = AutomationRecapStore(self.automation_store_path)
        try:
            counts = {"new": 0, "seen": 0, "expired": 0}
            if refresh:
                from signals.nasdaq import (  # type: ignore[import-untyped]
                    SOURCE_NAME,
                    fetch_splits_calendar,
                    parse_splits_payload,
                )

                if self.calendar_fetcher is not None:
                    signals = await self.calendar_fetcher()
                else:
                    signals = parse_splits_payload(await fetch_splits_calendar())
                counts.update(
                    store.upsert_calendar_signals(signals, source=SOURCE_NAME, now=now)
                )
                counts["expired"] = store.expire_stale_calendar_signals(
                    today=now.date(), now=now
                )
            rows = [
                {key: row[key] for key in row.keys()}
                for row in store.list_calendar_signals(status="new")
            ]
            return {"ok": True, "counts": counts, "signals": rows}
        finally:
            store.conn.close()

    @logged_tool(tool="router.dismiss_signal")
    async def dismiss_signal(self, signal_id: int, reason: str) -> dict[str, Any]:
        """Mark a calendar signal dismissed with a reason (audit trail)."""
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        store = AutomationRecapStore(self.automation_store_path)
        try:
            store.dismiss_calendar_signal(signal_id, reason=reason, now=_dt.now())
            return {"ok": True, "signal_id": signal_id, "status": "dismissed"}
        finally:
            store.conn.close()

    @logged_tool(tool="router.promote_signal")
    async def promote_signal(self, signal_id: int) -> dict[str, Any]:
        """Promote a calendar signal into the actionable buy_signals queue.
        Does NOT place any order — the buy still flows through the normal
        propose/execute gates."""
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        store = AutomationRecapStore(self.automation_store_path)
        try:
            try:
                buy_id = store.promote_calendar_signal(signal_id, now=_dt.now())
            except ValueError as exc:
                return {"ok": False, "signal_id": signal_id, "error": str(exc)}
            return {"ok": True, "signal_id": signal_id, "buy_signal_id": buy_id}
        finally:
            store.conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/agentic/test_calendar_signals_router.py -v`
Expected: 5 PASS

- [ ] **Step 5: Add the FastMCP tool wrappers**

Inside `build_router_fastmcp_server`, after the `recap_ingest` tool (~line 992):

```python
    @app.tool()
    async def scan_signals(refresh: bool = True) -> dict[str, Any]:
        """Scan the Nasdaq reverse-split calendar into the signal store
        (refresh=True) or read staged 'new' signals (refresh=False).
        Read/ingest only — never trades. Evaluate each returned signal and
        either promote_signal (worth playing) or dismiss_signal (with reason).
        """
        return await router.scan_signals(refresh=refresh)

    @app.tool()
    async def dismiss_signal(signal_id: int, reason: str) -> dict[str, Any]:
        """Dismiss a calendar signal that isn't worth playing. Always give a
        concrete reason (e.g. 'ratio below 1:5', 'price exceeds per-order cap')
        — it's the audit trail for why the agent skipped a play."""
        return await router.dismiss_signal(signal_id=signal_id, reason=reason)

    @app.tool()
    async def promote_signal(signal_id: int) -> dict[str, Any]:
        """Promote a calendar signal to the actionable buy queue. This stages
        intent only — the buy itself still requires propose_order +
        principal approval + execute_order."""
        return await router.promote_signal(signal_id=signal_id)
```

- [ ] **Step 6: Verify MCP registration**

Check how `tests/agentic/test_fastmcp_runtime.py` asserts tool registration and extend it (or assert inline in the new test file) that `scan_signals`, `dismiss_signal`, `promote_signal` appear in the FastMCP app's tool list.

Run: `.venv/bin/python -m pytest tests/agentic/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/agentic/router/_server.py tests/agentic/test_calendar_signals_router.py
git commit -m "feat(router): scan/dismiss/promote signal tools on ssg-router MCP"
```

---

### Task 6: `status` command (Pulse feed)

**Files:**
- Create: `src/cli/status.py`
- Modify: `src/main.py` (subparser + dispatch; `known_actions` already updated in Task 4)
- Create: `tests/test_cli_status.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_status.py
import argparse
import asyncio
import json
from datetime import datetime

from automation_recap import AutomationRecapStore
from cli.status import run_status
from rsa_store import RsaStore
from signals.nasdaq import CalendarSignal
from sweep import SweepStatus

NOW = datetime(2026, 7, 1, 12, 0, 0)


def _args(tmp_path):
    return argparse.Namespace(db_path=str(tmp_path / "automation.sqlite3"), json=True)


def _seed(tmp_path):
    db = str(tmp_path / "automation.sqlite3")
    rsa = RsaStore(db)
    trade_id = rsa.create_trade("ABCD", "1:25", expected_split_date="2026-07-14", now=NOW)
    pos_id = rsa.add_position(trade_id, "Fennel", "acct1", 1, now=NOW)
    rsa.record_sweep(pos_id, SweepStatus.AWAITING_SPLIT, 1.0, 1, NOW.isoformat())
    rsa.close()

    auto = AutomationRecapStore(db)
    auto.upsert_calendar_signals(
        [CalendarSignal(ticker="ZZZZ", ratio="1:8",
                        effective_date="2099-02-01", company="", raw={})],
        source="nasdaq_calendar", now=NOW,
    )
    auto.conn.close()
    return trade_id


def test_status_json_snapshot(tmp_path, capsys):
    trade_id = _seed(tmp_path)
    exit_code = asyncio.run(run_status(_args(tmp_path)))
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)

    assert out["ok"] is True
    assert out["generated_at"]  # ISO timestamp
    trade = out["trades"][0]
    assert trade["id"] == trade_id
    assert trade["ticker"] == "ABCD"
    assert trade["positions"][0]["broker"] == "Fennel"
    assert trade["positions"][0]["status"] == "awaiting_split"
    assert out["calendar_signals"]["new"] == 1
    assert out["buy_signals"]["pending"] == 0


def test_status_empty_db_is_valid(tmp_path, capsys):
    exit_code = asyncio.run(run_status(_args(tmp_path)))
    assert exit_code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trades"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_status.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'cli.status'`

- [ ] **Step 3: Implement the handler**

```python
# src/cli/status.py
"""`status` command: aggregate JSON snapshot of RSA state for external
consumers (the PAI Pulse dashboard polls this). Read-only."""

from __future__ import annotations

import json
from datetime import datetime

from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]
from rsa_store import RsaStore  # type: ignore[import-untyped]


def _count_by(conn, table: str, column: str) -> dict[str, int]:
    rows = conn.execute(
        f"SELECT {column} AS k, COUNT(*) AS n FROM {table} GROUP BY {column}"
    ).fetchall()
    return {row["k"]: row["n"] for row in rows}


async def run_status(args) -> int:
    rsa = RsaStore(args.db_path)
    auto = AutomationRecapStore(args.db_path)
    try:
        trades = []
        for trade_row in rsa.conn.execute(
            "SELECT * FROM rsa_trades ORDER BY id DESC"
        ).fetchall():
            positions = [
                {
                    "broker": p["broker"],
                    "account_id": p["account_id"],
                    "pre_split_qty": p["pre_split_qty"],
                    "status": p["status"],
                    "observed_qty": p["observed_qty"],
                    "sold_at": p["sold_at"],
                }
                for p in rsa.list_positions(trade_row["id"])
            ]
            trades.append(
                {
                    "id": trade_row["id"],
                    "ticker": trade_row["ticker"],
                    "split_ratio": trade_row["split_ratio"],
                    "expected_split_date": trade_row["expected_split_date"],
                    "created_at": trade_row["created_at"],
                    "positions": positions,
                }
            )

        snapshot = {
            "ok": True,
            "generated_at": datetime.now().isoformat(),
            "trades": trades,
            "calendar_signals": _count_by(auto.conn, "calendar_signals", "status"),
            "buy_signals": _count_by(auto.conn, "buy_signals", "status"),
            "pending_sell_triggers": _count_by(
                auto.conn, "pending_sell_triggers", "status"
            ),
        }
        # Guarantee stable keys even when tables are empty
        snapshot["calendar_signals"].setdefault("new", 0)
        snapshot["buy_signals"].setdefault("pending", 0)

        print(json.dumps(snapshot, indent=2))
        return 0
    finally:
        rsa.close()
        auto.conn.close()
```

Note both stores open the same SQLite file (`logs/automation.sqlite3` by default) — that's the existing convention (`DEFAULT_RSA_STORE_PATH == DEFAULT_AUTOMATION_STORE_PATH` in `_server.py:31-32`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_status.py -v`
Expected: 2 PASS

- [ ] **Step 5: Wire into `main.py`**

Subparser (after the `signals` parser from Task 4):

```python
    status_parser = subparsers.add_parser(
        "status",
        help="Aggregate JSON snapshot of RSA state (for Pulse/monitoring)",
    )
    status_parser.add_argument("--json", action="store_true", default=True, help="JSON output (always on)")
    status_parser.add_argument(
        "--db-path",
        default=db_default,
        help="SQLite path for automation state",
    )
```

Dispatch:

```python
    if args.action == "status":
        from cli.status import run_status

        return await run_status(args)
```

Smoke test: `.venv/bin/python src/main.py status --db-path /tmp/ssg-signals-test.sqlite3` → valid JSON, exit 0.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/cli/status.py src/main.py tests/test_cli_status.py
git commit -m "feat(cli): status command emitting aggregate JSON snapshot for Pulse"
```

---

### Task 7: docs + wrap-up

**Files:**
- Modify: `CLAUDE.md` (Project Overview / commands), `CONTEXT.md` (if it documents commands)
- Modify: `specs/agent-operated-rsa-engine.md` (mark Plan 1 components shipped)

- [ ] **Step 1: Document the new commands**

Add `signals scan|list` and `status` to the "Running the Application" section of `CLAUDE.md`, and the three new MCP tools to wherever the router tools are listed (check `docs/agentic/`). Follow existing doc style.

- [ ] **Step 2: Full verification**

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m py_compile src/main.py src/cli/*.py src/signals/*.py src/automation_recap.py
```

Expected: all pass, no compile errors.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md CONTEXT.md docs specs/agent-operated-rsa-engine.md
git commit -m "docs: signals + status commands, router signal tools"
```
