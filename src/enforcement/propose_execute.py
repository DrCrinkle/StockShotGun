from __future__ import annotations

import secrets
import sqlite3
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from enforcement.errors import (
    IntentMismatch,
    TokenAlreadyUsed,
    TokenExpired,
    TokenInvalid,
)
from enforcement.intent import intent_hash
from enforcement.types import BrokerAccount, LegProposal, OrderIntent, OrderSide, Proposal


class ProposalStore:
    """SQLite-backed per-leg proposal store (v0.3 schema).

    A fan-out proposal stores ONE row in `proposals` (the master record) and
    N rows in `leg_proposals` (one per (broker, account_id) target). Each leg
    row carries its own random 256-bit token, the SHA-256 of a single-target
    `OrderIntent`, and an independent `consumed_at` timestamp. A broker MCP
    validates ITS leg token against ITS single-target intent — fan-out works
    cleanly through MCP stdio, subprocess isolation works, partial failure is
    a first-class outcome.
    """

    def __init__(self, db_path: str | Path):
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate_pre_f2c_schema()
        self._init_schema()

    def _migrate_pre_f2c_schema(self) -> None:
        """Drop pre-F2c schema if detected. Pre-F2c had a flat `proposals`
        table with a single `token` column; F2c uses `proposal_id` PK + a
        separate `leg_proposals` child table. `CREATE TABLE IF NOT EXISTS`
        silently keeps the old shape, breaking inserts at runtime — detect
        and drop. Proposals are short-lived (300s TTL by default) so dropping
        active rows is acceptable; the operator just re-proposes.
        """
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='proposals'"
        ).fetchone()
        if row is None:
            return
        cols = {
            r["name"]
            for r in self.conn.execute("PRAGMA table_info(proposals)").fetchall()
        }
        # Drop on either the pre-F2c shape (no proposal_id) OR the pre-ADR-0004
        # shape (no stored order params). Proposals are 300s-ephemeral, so
        # dropping active rows on a shape change is acceptable — the operator
        # just re-proposes.
        if "proposal_id" not in cols or "ticker" not in cols:
            self.conn.executescript(
                "DROP TABLE IF EXISTS leg_proposals;\n"
                "DROP TABLE IF EXISTS proposals;\n"
            )
            self.conn.commit()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposals (
                proposal_id TEXT PRIMARY KEY,
                valid_until_ts REAL NOT NULL,
                estimated_usd REAL NOT NULL,
                created_ts REAL NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                price REAL,
                dry_run INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leg_proposals (
                token TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL REFERENCES proposals(proposal_id),
                intent_hash TEXT NOT NULL,
                broker TEXT NOT NULL,
                account_id TEXT NOT NULL,
                valid_until_ts REAL NOT NULL,
                estimated_usd REAL NOT NULL,
                created_ts REAL NOT NULL,
                consumed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_leg_proposal_id
                ON leg_proposals(proposal_id);
            CREATE INDEX IF NOT EXISTS idx_leg_valid_until
                ON leg_proposals(valid_until_ts);
            """
        )
        self.conn.commit()

    def insert(self, proposal: Proposal) -> None:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                self.conn.execute(
                    """
                    INSERT INTO proposals(proposal_id, valid_until_ts,
                                          estimated_usd, created_ts,
                                          ticker, side, qty, price, dry_run)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        proposal.proposal_id,
                        proposal.valid_until_ts,
                        proposal.estimated_usd,
                        proposal.created_ts,
                        proposal.ticker,
                        proposal.side.value,
                        proposal.qty,
                        proposal.price,
                        1 if proposal.dry_run else 0,
                    ),
                )
                for leg in proposal.legs:
                    self.conn.execute(
                        """
                        INSERT INTO leg_proposals(token, proposal_id, intent_hash,
                                                  broker, account_id,
                                                  valid_until_ts, estimated_usd,
                                                  created_ts)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            leg.token,
                            leg.proposal_id,
                            leg.intent_hash,
                            leg.broker,
                            leg.account_id,
                            leg.valid_until_ts,
                            leg.estimated_usd,
                            leg.created_ts,
                        ),
                    )
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    def get_proposal(self, proposal_id: str) -> Proposal | None:
        with self._lock:
            head = self.conn.execute(
                "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if head is None:
                return None
            legs_rows = self.conn.execute(
                """
                SELECT * FROM leg_proposals WHERE proposal_id = ?
                ORDER BY broker, account_id
                """,
                (proposal_id,),
            ).fetchall()
            legs = tuple(_row_to_leg(r) for r in legs_rows)
            return Proposal(
                proposal_id=head["proposal_id"],
                legs=legs,
                valid_until_ts=float(head["valid_until_ts"]),
                estimated_usd=float(head["estimated_usd"]),
                created_ts=float(head["created_ts"]),
                ticker=head["ticker"],
                side=OrderSide(head["side"]),
                qty=float(head["qty"]),
                price=None if head["price"] is None else float(head["price"]),
                dry_run=bool(head["dry_run"]),
            )

    def consume_leg(self, leg_token: str, supplied_intent_hash: str) -> LegProposal:
        """Validate and atomically mark this leg consumed.

        Raises `TokenInvalid` / `TokenExpired` / `TokenAlreadyUsed` /
        `IntentMismatch`. The validation + consume happen inside a single
        transaction; concurrent second-consume calls on the same leg token
        deterministically lose the race.
        """
        now = time.time()
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                row = self.conn.execute(
                    "SELECT * FROM leg_proposals WHERE token = ?", (leg_token,)
                ).fetchone()
                if row is None:
                    raise TokenInvalid("leg confirmation token not recognized")
                if row["consumed_at"] is not None:
                    raise TokenAlreadyUsed("leg confirmation token already consumed")
                if now >= float(row["valid_until_ts"]):
                    raise TokenExpired("leg confirmation token expired")
                if row["intent_hash"] != supplied_intent_hash:
                    raise IntentMismatch(
                        "execute intent does not match propose intent (per-leg)"
                    )
                self.conn.execute(
                    "UPDATE leg_proposals SET consumed_at = ? WHERE token = ?",
                    (datetime.now(UTC).isoformat(), leg_token),
                )
                self.conn.commit()
                return _row_to_leg(row)
            except Exception:
                self.conn.rollback()
                raise


def _row_to_leg(row: sqlite3.Row) -> LegProposal:
    return LegProposal(
        token=row["token"],
        intent_hash=row["intent_hash"],
        broker=row["broker"],
        account_id=row["account_id"],
        proposal_id=row["proposal_id"],
        valid_until_ts=float(row["valid_until_ts"]),
        estimated_usd=float(row["estimated_usd"]),
        created_ts=float(row["created_ts"]),
    )


def propose_fanout(
    *,
    intent: OrderIntent,
    estimated_usd_total: float,
    estimated_usd_per_leg: dict[BrokerAccount, float],
    store: ProposalStore,
    ttl_seconds: float,
) -> Proposal:
    """Mint one proposal with N leg sub-proposals — one per intent target.

    Each leg gets its own random token bound to a SINGLE-TARGET `OrderIntent`
    (the same broker/account, but with `targets=(this_leg,)`). Brokers
    validate against THIS hash at execute time, which is the hash they would
    naturally compute from their own single-target view of the order.
    """
    now = time.time()
    proposal_id = uuid.uuid4().hex
    valid_until = now + ttl_seconds
    legs: list[LegProposal] = []
    for target in intent.targets:
        single_leg_intent = OrderIntent(
            ticker=intent.ticker,
            side=intent.side,
            qty=intent.qty,
            targets=(target,),
            price=intent.price,
            dry_run=intent.dry_run,
        )
        leg_hash = intent_hash(single_leg_intent)
        leg = LegProposal(
            token=secrets.token_urlsafe(48),
            intent_hash=leg_hash,
            broker=target.broker,
            account_id=target.account_id,
            proposal_id=proposal_id,
            valid_until_ts=valid_until,
            estimated_usd=estimated_usd_per_leg.get(target, 0.0),
            created_ts=now,
        )
        legs.append(leg)
    # Invariant: the master order params we store must reproduce EVERY leg's
    # bound intent_hash. True by construction here (all legs derive from one
    # `intent`), but assert it so a future change that varies params per leg —
    # making a single stored param set a lie for some legs — fails loudly
    # instead of silently persisting the wrong qty/price.
    for leg in legs:
        expected = intent_hash(
            OrderIntent(
                ticker=intent.ticker,
                side=intent.side,
                qty=intent.qty,
                targets=(BrokerAccount(leg.broker, leg.account_id),),
                price=intent.price,
                dry_run=intent.dry_run,
            )
        )
        if leg.intent_hash != expected:
            raise ValueError(
                "proposal legs do not all share the master order params; "
                "store params per-leg or split into separate proposals"
            )
    proposal = Proposal(
        proposal_id=proposal_id,
        legs=tuple(legs),
        valid_until_ts=valid_until,
        estimated_usd=estimated_usd_total,
        created_ts=now,
        ticker=intent.ticker,
        side=intent.side,
        qty=intent.qty,
        price=intent.price,
        dry_run=intent.dry_run,
    )
    store.insert(proposal)
    return proposal


def validate_leg_for_execute(
    leg_token: str,
    single_leg_intent: OrderIntent,
    *,
    store: ProposalStore,
) -> LegProposal:
    """Consume one leg's token and confirm it binds to its single-leg intent."""
    return store.consume_leg(leg_token, intent_hash(single_leg_intent))
