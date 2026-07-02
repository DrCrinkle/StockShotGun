from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol


BROKER_ALIAS_MAP = {
    "rh": "Robinhood",
    "robinhood": "Robinhood",
    "schwab": "Schwab",
    "fidelity": "Fidelity",
    "tradier": "Tradier",
    "tastytrade": "TastyTrade",
    "tasty": "TastyTrade",
    "public": "Public",
    "firstrade": "Firstrade",
    "fennel": "Fennel",
    "bbae": "BBAE",
    "dspac": "DSPAC",
    "sofi": "SoFi",
    "webull": "Webull",
    "wellsfargo": "WellsFargo",
    "wells": "WellsFargo",
    "chase": "Chase",
}


@dataclass(slots=True)
class UpcomingBuy:
    ticker: str
    date_mmdd: str | None
    ratio: str | None
    round_num: int | None
    notes: str
    raw_line: str


@dataclass(slots=True)
class StockBackItem:
    ticker: str
    detail: str
    brokers: list[str]
    raw_line: str


@dataclass(slots=True)
class ResearchSignal:
    """A ticker mentioned in the `RESEARCH POSTED` section — early signal.

    Date is the day research was posted (or the announced split date if the
    chat uses the same date for both). Not committed to a buy yet; the agent
    monitors these and promotes to a buy signal when ratio + firm date arrive.
    """

    ticker: str
    date_mmdd: str | None
    notes: str
    raw_line: str


@dataclass(slots=True)
class TBACandidate:
    """A ticker under the `TBA` section — known event, ratio and/or date pending.

    Common notes fields: `OTC`, `merger`, `ADR`, `delayed`, `round down`. The
    agent treats these as a long watchlist; promotion to an upcoming buy
    happens when the chat moves the ticker into `UPCOMING BUYS` with a
    firm date + ratio.
    """

    ticker: str
    ratio: str | None
    notes: str
    raw_line: str


@dataclass(slots=True)
class RecapParseResult:
    """Full result of `parse_chat_recap_full` — all four signal tiers."""

    upcoming: list[UpcomingBuy]
    stock_back: list[StockBackItem]
    research: list[ResearchSignal]
    tba: list[TBACandidate]


def _line_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_mmdd(value: str | None) -> tuple[int, int] | None:
    if not value or not re.fullmatch(r"\d{2}/\d{2}", value):
        return None

    month, day = (int(part) for part in value.split("/", 1))
    try:
        date(2000, month, day)
    except ValueError:
        return None
    return month, day


def _resolve_target_date(created_date: date, month: int, day: int) -> date | None:
    for year in range(created_date.year, created_date.year + 8):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= created_date:
            return candidate
    return None


def _is_due_buy_signal(created_at: str, target_mmdd: str | None, today: date) -> bool:
    if not target_mmdd:
        return True

    parsed_target = _parse_mmdd(target_mmdd)
    if parsed_target is None:
        return False

    try:
        created_date = datetime.fromisoformat(created_at).date()
    except ValueError:
        return False

    month, day = parsed_target
    target_date = _resolve_target_date(created_date, month, day)
    if target_date is None:
        return False

    return target_date <= today


def parse_chat_recap(text: str) -> tuple[list[UpcomingBuy], list[StockBackItem]]:
    """Legacy 2-tuple signature — used by main.py's automate path. Returns only
    the actionable buy signals + stock-back state. For the full 4-tier parse
    (upcoming + stock_back + research + tba), call `parse_chat_recap_full`.
    """
    result = parse_chat_recap_full(text)
    return result.upcoming, result.stock_back


def parse_chat_recap_full(text: str) -> RecapParseResult:
    """Parse all four signal tiers from a chat recap.

    Section grammar:
      - Section headers: lines that are `-TEXT-` (uppercase, no slashes)
      - Date subheaders: lines that are `-MM/DD-` — set the active date inside
        a section that uses date grouping (UPCOMING BUYS, RESEARCH POSTED)
      - Ticker lines: `TICKER - field - field - ...`, where a `N:D` part is
        the ratio, `round N` is the round number, and everything else is notes
      - Comment lines: start with `*` — skipped
    """
    section = ""
    active_date: str | None = None
    upcoming: list[UpcomingBuy] = []
    stock_back: list[StockBackItem] = []
    research: list[ResearchSignal] = []
    tba: list[TBACandidate] = []

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("-") and line.endswith("-"):
            header = line.strip("-").strip().lower()
            if re.fullmatch(r"\d{2}/\d{2}", header):
                active_date = header
            else:
                section = header
                active_date = None
            continue

        if line.startswith("*"):
            continue

        if section == "upcoming buys":
            parts = [part.strip() for part in line.split(" - ")]
            if not parts:
                continue
            ticker = parts[0].upper()
            ratio = None
            round_num = None
            notes = ""

            for part in parts[1:]:
                if re.fullmatch(r"\d+:\d+", part):
                    ratio = part
                    continue
                round_match = re.search(r"round\s*(\d+)", part, re.IGNORECASE)
                if round_match:
                    round_num = int(round_match.group(1))
                    continue
                notes = f"{notes} | {part}" if notes else part

            upcoming.append(
                UpcomingBuy(
                    ticker=ticker,
                    date_mmdd=active_date,
                    ratio=ratio,
                    round_num=round_num,
                    notes=notes,
                    raw_line=line,
                )
            )
            continue

        if section == "stocks back and latest":
            parts = [part.strip() for part in line.split(" - ")]
            if not parts:
                continue
            ticker = parts[0].upper()
            detail = " - ".join(parts[1:]) if len(parts) > 1 else ""
            brokers = _extract_brokers(detail)
            stock_back.append(
                StockBackItem(
                    ticker=ticker,
                    detail=detail,
                    brokers=brokers,
                    raw_line=line,
                )
            )

        if section == "research posted":
            parts = [part.strip() for part in line.split(" - ")]
            if not parts:
                continue
            ticker = parts[0].upper()
            notes = " | ".join(parts[1:]) if len(parts) > 1 else ""
            research.append(
                ResearchSignal(
                    ticker=ticker,
                    date_mmdd=active_date,
                    notes=notes,
                    raw_line=line,
                )
            )

        if section == "tba":
            parts = [part.strip() for part in line.split(" - ")]
            if not parts:
                continue
            ticker = parts[0].upper()
            ratio: str | None = None
            note_parts: list[str] = []
            for part in parts[1:]:
                # Find the ratio anywhere in the segment — TBA entries like
                # "ENZN - OTC - 1:100 merger" carry the ratio embedded in notes.
                ratio_match = re.search(r"\b(\d+:\d+)\b", part)
                if ratio is None and ratio_match:
                    ratio = ratio_match.group(1)
                    remainder = part.replace(ratio_match.group(1), "").strip()
                    if remainder:
                        note_parts.append(remainder)
                else:
                    note_parts.append(part)
            tba.append(
                TBACandidate(
                    ticker=ticker,
                    ratio=ratio,
                    notes=" | ".join(p for p in note_parts if p),
                    raw_line=line,
                )
            )

    return RecapParseResult(
        upcoming=upcoming,
        stock_back=stock_back,
        research=research,
        tba=tba,
    )


def _extract_brokers(detail: str) -> list[str]:
    found = []
    lowered = detail.lower()
    for alias, broker in BROKER_ALIAS_MAP.items():
        pattern = rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)"
        if re.search(pattern, lowered) and broker not in found:
            found.append(broker)
    return found


class CalendarSignalLike(Protocol):
    """Structural contract for calendar-signal ingestion. Deliberately a
    local Protocol rather than an import from `signals` — the store must
    not depend on source-adapter packages."""

    ticker: str
    ratio: str
    effective_date: str | None
    raw: dict[str, Any]


class AutomationRecapStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS recap_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                recap_hash TEXT NOT NULL,
                raw_text TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS stock_back_state (
                ticker TEXT PRIMARY KEY,
                detail_hash TEXT NOT NULL,
                detail_text TEXT NOT NULL,
                brokers_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_sell_triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                reason TEXT NOT NULL,
                brokers_json TEXT NOT NULL,
                source_hash TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS buy_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                target_date TEXT,
                ratio TEXT,
                round_num INTEGER,
                notes TEXT,
                signal_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                executed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS research_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                target_date TEXT,
                notes TEXT,
                signal_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_at TEXT
            );

            CREATE TABLE IF NOT EXISTS tba_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                ratio TEXT,
                notes TEXT,
                signal_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                promoted_at TEXT
            );

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
            """
        )
        self.conn.commit()

    def record_recap(
        self,
        raw_text: str,
        upcoming: list[UpcomingBuy],
        stock_back: list[StockBackItem],
        now: datetime,
    ) -> dict[str, int]:
        now_iso = now.isoformat()
        recap_hash = _line_hash(raw_text)
        self.conn.execute(
            "INSERT INTO recap_snapshots(created_at, recap_hash, raw_text) VALUES (?, ?, ?)",
            (now_iso, recap_hash, raw_text),
        )

        new_buy = 0
        new_sell = 0

        for signal in upcoming:
            key = _line_hash(
                f"{signal.ticker}|{signal.date_mmdd}|{signal.ratio}|{signal.round_num}|{signal.raw_line}"
            )
            try:
                self.conn.execute(
                    """
                    INSERT INTO buy_signals(
                        ticker, target_date, ratio, round_num, notes, signal_key, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        signal.ticker,
                        signal.date_mmdd,
                        signal.ratio,
                        signal.round_num,
                        signal.notes,
                        key,
                        now_iso,
                    ),
                )
                new_buy += 1
            except sqlite3.IntegrityError:
                pass

        for item in stock_back:
            detail_hash = _line_hash(item.detail)
            existing = self.conn.execute(
                "SELECT detail_hash FROM stock_back_state WHERE ticker = ?",
                (item.ticker,),
            ).fetchone()

            should_trigger = existing is None or existing["detail_hash"] != detail_hash
            self.conn.execute(
                """
                INSERT INTO stock_back_state(ticker, detail_hash, detail_text, brokers_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    detail_hash=excluded.detail_hash,
                    detail_text=excluded.detail_text,
                    brokers_json=excluded.brokers_json,
                    updated_at=excluded.updated_at
                """,
                (
                    item.ticker,
                    detail_hash,
                    item.detail,
                    json.dumps(item.brokers),
                    now_iso,
                ),
            )

            if should_trigger:
                reason = "new" if existing is None else "changed"
                source_hash = _line_hash(f"{item.ticker}|{reason}|{detail_hash}")
                try:
                    self.conn.execute(
                        """
                        INSERT INTO pending_sell_triggers(
                            ticker, reason, brokers_json, source_hash, status, created_at
                        ) VALUES (?, ?, ?, ?, 'pending', ?)
                        """,
                        (
                            item.ticker,
                            reason,
                            json.dumps(item.brokers),
                            source_hash,
                            now_iso,
                        ),
                    )
                    new_sell += 1
                except sqlite3.IntegrityError:
                    pass

        self.conn.commit()
        return {"new_buy_signals": new_buy, "new_sell_triggers": new_sell}

    def record_recap_extended(
        self,
        raw_text: str,
        result: RecapParseResult,
        now: datetime,
    ) -> dict[str, int]:
        """Extended record_recap: also persists research signals + TBA candidates.

        Returns a dict with `new_buy`, `new_research`, `new_tba` counts (plus
        whatever `record_recap` returns from its existing logic).
        """
        base = self.record_recap(raw_text, result.upcoming, result.stock_back, now)
        now_iso = now.isoformat()

        new_research = 0
        for sig in result.research:
            key = _line_hash(
                f"research|{sig.ticker}|{sig.date_mmdd}|{sig.raw_line}"
            )
            try:
                self.conn.execute(
                    """
                    INSERT INTO research_signals(
                        ticker, target_date, notes, signal_key, status, created_at
                    ) VALUES (?, ?, ?, ?, 'active', ?)
                    """,
                    (sig.ticker, sig.date_mmdd, sig.notes, key, now_iso),
                )
                new_research += 1
            except sqlite3.IntegrityError:
                pass

        new_tba = 0
        for cand in result.tba:
            key = _line_hash(
                f"tba|{cand.ticker}|{cand.ratio}|{cand.raw_line}"
            )
            try:
                self.conn.execute(
                    """
                    INSERT INTO tba_candidates(
                        ticker, ratio, notes, signal_key, status, created_at
                    ) VALUES (?, ?, ?, ?, 'active', ?)
                    """,
                    (cand.ticker, cand.ratio, cand.notes, key, now_iso),
                )
                new_tba += 1
            except sqlite3.IntegrityError:
                pass

        self.conn.commit()
        base["new_research"] = new_research
        base["new_tba"] = new_tba
        return base

    def get_active_research_signals(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM research_signals WHERE status = 'active' "
                "ORDER BY target_date IS NULL, target_date, ticker"
            ).fetchall()
        )

    def get_active_tba_candidates(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM tba_candidates WHERE status = 'active' "
                "ORDER BY ratio IS NULL, ticker"
            ).fetchall()
        )

    def mark_research_promoted(self, signal_ids: list[int], now: datetime) -> None:
        """Mark research signals as promoted (e.g., when they appear in
        UPCOMING BUYS in a subsequent recap)."""
        if not signal_ids:
            return
        placeholders = ",".join("?" * len(signal_ids))
        self.conn.execute(
            f"UPDATE research_signals SET status = 'promoted', "
            f"promoted_at = ? WHERE id IN ({placeholders})",
            [now.isoformat()] + signal_ids,
        )
        self.conn.commit()

    def mark_tba_promoted(self, signal_ids: list[int], now: datetime) -> None:
        if not signal_ids:
            return
        placeholders = ",".join("?" * len(signal_ids))
        self.conn.execute(
            f"UPDATE tba_candidates SET status = 'promoted', "
            f"promoted_at = ? WHERE id IN ({placeholders})",
            [now.isoformat()] + signal_ids,
        )
        self.conn.commit()

    def get_due_buy_signals(self, today: date) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM buy_signals WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()
        return [
            row
            for row in rows
            if _is_due_buy_signal(row["created_at"], row["target_date"], today)
        ]

    def get_pending_sell_triggers(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM pending_sell_triggers WHERE status = 'pending' ORDER BY id ASC"
        ).fetchall()

    def mark_buy_signals_executed(self, signal_ids: list[int], now: datetime) -> None:
        if not signal_ids:
            return
        placeholders = ",".join("?" for _ in signal_ids)
        self.conn.execute(
            f"UPDATE buy_signals SET status='executed', executed_at=? WHERE id IN ({placeholders})",
            (now.isoformat(), *signal_ids),
        )
        self.conn.commit()

    def mark_sell_triggers_executed(
        self, trigger_ids: list[int], now: datetime
    ) -> None:
        if not trigger_ids:
            return
        placeholders = ",".join("?" for _ in trigger_ids)
        self.conn.execute(
            f"UPDATE pending_sell_triggers SET status='executed', executed_at=? WHERE id IN ({placeholders})",
            (now.isoformat(), *trigger_ids),
        )
        self.conn.commit()

    # --- calendar signals (automated source feed, e.g. Nasdaq calendar) ---

    def upsert_calendar_signals(
        self,
        signals: list[CalendarSignalLike],
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
            # SELECT-then-INSERT (not ON CONFLICT) because the new/seen count
            # split needs to know existence up front; fine at calendar scale.
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
                "SELECT * FROM calendar_signals "
                "ORDER BY effective_date IS NULL, effective_date, ticker"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM calendar_signals WHERE status = ? "
            "ORDER BY effective_date IS NULL, effective_date, ticker",
            (status,),
        ).fetchall()

    def dismiss_calendar_signal(self, signal_id: int, reason: str, now: datetime) -> None:
        """Dismiss a 'new' calendar signal. Raises `ValueError` if the signal
        doesn't exist, and raises `ValueError` if it isn't in 'new' status —
        in particular this prevents dismissing an already-'promoted' signal,
        which would orphan its pending buy_signals row."""
        with self.conn:
            row = self.conn.execute(
                "SELECT * FROM calendar_signals WHERE id = ?", (signal_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"calendar signal {signal_id} not found")
            if row["status"] != "new":
                raise ValueError(
                    f"calendar signal {signal_id} is '{row['status']}', not 'new'"
                )
            self.conn.execute(
                "UPDATE calendar_signals SET status = 'dismissed', "
                "dismissed_reason = ?, last_seen = ? WHERE id = ?",
                (reason, now.isoformat(), signal_id),
            )

    def promote_calendar_signal(self, signal_id: int, now: datetime) -> int:
        """Promote a 'new' calendar signal into the actionable buy_signals
        queue (consumed by the automate due-buy path). Returns buy_signal id.

        If the signal's `effective_date` is None, the created buy_signals row
        gets `target_date` NULL — and `_is_due_buy_signal` treats a NULL
        target as immediately due, so a date-less promotion becomes an
        immediately-due buy in the automate path."""
        with self.conn:
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
        """Mark 'new' signals whose effective date has passed as expired.
        Signals with a NULL effective_date are never touched — there is no
        date to compare against, so they can't be considered stale."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE calendar_signals SET status = 'expired', last_seen = ? "
                "WHERE status = 'new' AND effective_date IS NOT NULL "
                "AND effective_date < ?",
                (now.isoformat(), today.isoformat()),
            )
        return cursor.rowcount

    def close(self) -> None:
        self.conn.close()
