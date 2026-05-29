from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from sweep import SweepStatus


class RsaStore:
    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS rsa_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                split_ratio TEXT NOT NULL,
                expected_split_date TEXT,
                signal_id INTEGER,
                notes TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS rsa_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL REFERENCES rsa_trades(id),
                broker TEXT NOT NULL,
                account_id TEXT NOT NULL DEFAULT '',
                pre_split_qty INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(trade_id, broker, account_id)
            );

            CREATE TABLE IF NOT EXISTS sweep_state (
                position_id INTEGER PRIMARY KEY REFERENCES rsa_positions(id),
                status TEXT NOT NULL,
                observed_qty REAL,
                expected_post_qty INTEGER,
                first_checked TEXT NOT NULL,
                last_checked TEXT NOT NULL,
                sold_at TEXT
            );

            CREATE TABLE IF NOT EXISTS sweep_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id INTEGER NOT NULL REFERENCES rsa_positions(id),
                observed_at TEXT NOT NULL,
                status TEXT NOT NULL,
                observed_qty REAL,
                expected_post_qty INTEGER,
                details TEXT
            );
            """
        )
        self.conn.commit()

    def create_trade(
        self,
        ticker: str,
        split_ratio: str,
        expected_split_date: str | None = None,
        signal_id: int | None = None,
        notes: str | None = None,
        now: datetime | None = None,
    ) -> int:
        created_at = (now or datetime.now()).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO rsa_trades(ticker, split_ratio, expected_split_date, signal_id, notes, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ticker, split_ratio, expected_split_date, signal_id, notes, created_at),
        )
        self.conn.commit()
        trade_id = cursor.lastrowid
        assert trade_id is not None
        return trade_id

    def add_position(
        self,
        trade_id: int,
        broker: str,
        account_id: str | None,
        pre_split_qty: int,
        now: datetime | None = None,
    ) -> int:
        created_at = (now or datetime.now()).isoformat()
        cursor = self.conn.execute(
            """
            INSERT INTO rsa_positions(trade_id, broker, account_id, pre_split_qty, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (trade_id, broker, account_id or "", pre_split_qty, created_at),
        )
        self.conn.commit()
        position_id = cursor.lastrowid
        assert position_id is not None
        return position_id

    def record_sweep(
        self,
        position_id: int,
        status: SweepStatus,
        observed_qty: float | None,
        expected_post_qty: int | None,
        observed_at: str,
        details: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO sweep_state(
                    position_id, status, observed_qty, expected_post_qty,
                    first_checked, last_checked
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_id) DO UPDATE SET
                    status = excluded.status,
                    observed_qty = excluded.observed_qty,
                    expected_post_qty = excluded.expected_post_qty,
                    last_checked = excluded.last_checked
                """,
                (
                    position_id,
                    status,
                    observed_qty,
                    expected_post_qty,
                    observed_at,
                    observed_at,
                ),
            )
            self.conn.execute(
                """
                INSERT INTO sweep_history(
                    position_id, observed_at, status, observed_qty, expected_post_qty, details
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (position_id, observed_at, status, observed_qty, expected_post_qty, details),
            )

    def mark_sold(self, position_id: int, sold_at: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sweep_state SET sold_at = ? WHERE position_id = ?",
                (sold_at, position_id),
            )

    def get_trade(self, trade_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT id, ticker, split_ratio, expected_split_date, signal_id, notes, created_at "
            "FROM rsa_trades WHERE id = ?",
            (trade_id,),
        ).fetchone()

    def get_raw_positions(self, trade_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, trade_id, broker, account_id, pre_split_qty, created_at "
            "FROM rsa_positions WHERE trade_id = ? ORDER BY id",
            (trade_id,),
        ).fetchall()

    def list_positions(self, trade_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT
                p.id, p.trade_id, p.broker, p.account_id, p.pre_split_qty, p.created_at,
                s.status, s.observed_qty, s.expected_post_qty,
                s.first_checked, s.last_checked, s.sold_at
            FROM rsa_positions p
            LEFT JOIN sweep_state s ON s.position_id = p.id
            WHERE p.trade_id = ?
            ORDER BY p.id
            """,
            (trade_id,),
        ).fetchall()

    def close(self) -> None:
        self.conn.close()
