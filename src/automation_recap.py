from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


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
    target_date = date(created_date.year, month, day)
    if target_date < created_date:
        target_date = date(created_date.year + 1, month, day)

    return target_date <= today


def parse_chat_recap(text: str) -> tuple[list[UpcomingBuy], list[StockBackItem]]:
    section = ""
    active_date: str | None = None
    upcoming: list[UpcomingBuy] = []
    stock_back: list[StockBackItem] = []

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

    return upcoming, stock_back


def _extract_brokers(detail: str) -> list[str]:
    found = []
    lowered = detail.lower()
    for alias, broker in BROKER_ALIAS_MAP.items():
        pattern = rf"(^|[^a-z0-9]){re.escape(alias)}([^a-z0-9]|$)"
        if re.search(pattern, lowered) and broker not in found:
            found.append(broker)
    return found


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

    def close(self) -> None:
        self.conn.close()
