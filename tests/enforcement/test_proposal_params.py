"""ADR 0005 — order params persisted on the Proposal (store-level).

Covers: round-trip of ticker/side/qty/price/dry_run through ProposalStore
(including market orders where price=None), the invariant that the stored
master params reproduce every leg's bound intent_hash, and the schema
migration that drops a pre-params `proposals` table.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from enforcement.intent import intent_hash
from enforcement.propose_execute import ProposalStore, propose_fanout
from enforcement.types import BrokerAccount, OrderIntent, OrderSide


def _intent(price: float | None, dry_run: bool) -> OrderIntent:
    return OrderIntent(
        ticker="TSLA",
        side=OrderSide.BUY,
        qty=3.0,
        targets=(BrokerAccount("FakeA", "acc1"), BrokerAccount("FakeB", "acc2")),
        price=price,
        dry_run=dry_run,
    )


def test_params_round_trip_limit_order(tmp_path: Path):
    store = ProposalStore(tmp_path / "p.sqlite")
    intent = _intent(price=5.0, dry_run=False)
    proposal = propose_fanout(
        intent=intent,
        estimated_usd_total=30.0,
        estimated_usd_per_leg={t: 15.0 for t in intent.targets},
        store=store,
        ttl_seconds=60.0,
    )
    loaded = store.get_proposal(proposal.proposal_id)
    assert loaded is not None
    assert loaded.ticker == "TSLA"
    assert loaded.side == OrderSide.BUY
    assert loaded.qty == 3.0
    assert loaded.price == 5.0
    assert loaded.dry_run is False


def test_params_round_trip_market_order_price_none(tmp_path: Path):
    store = ProposalStore(tmp_path / "p.sqlite")
    intent = _intent(price=None, dry_run=True)
    proposal = propose_fanout(
        intent=intent,
        estimated_usd_total=0.0,
        estimated_usd_per_leg={t: 0.0 for t in intent.targets},
        store=store,
        ttl_seconds=60.0,
    )
    loaded = store.get_proposal(proposal.proposal_id)
    assert loaded is not None
    assert loaded.price is None  # market order survives as NULL → None
    assert loaded.dry_run is True


def test_stored_params_reproduce_every_leg_hash(tmp_path: Path):
    """The invariant the self-describing Proposal relies on: the master params,
    combined with each leg's (broker, account), hash to that leg's intent_hash.
    If this holds, execute's per-leg gate check will pass for params read from
    the store."""
    store = ProposalStore(tmp_path / "p.sqlite")
    intent = _intent(price=5.0, dry_run=False)
    proposal = propose_fanout(
        intent=intent,
        estimated_usd_total=30.0,
        estimated_usd_per_leg={t: 15.0 for t in intent.targets},
        store=store,
        ttl_seconds=60.0,
    )
    loaded = store.get_proposal(proposal.proposal_id)
    assert loaded is not None
    for leg in loaded.legs:
        recomputed = intent_hash(
            OrderIntent(
                ticker=loaded.ticker,
                side=loaded.side,
                qty=loaded.qty,
                targets=(BrokerAccount(leg.broker, leg.account_id),),
                price=loaded.price,
                dry_run=loaded.dry_run,
            )
        )
        assert recomputed == leg.intent_hash


def test_migration_drops_pre_params_proposals_table(tmp_path: Path):
    """A `proposals` table lacking the order-param columns is dropped and
    recreated (proposals are 300s-ephemeral, so dropping is acceptable)."""
    db = tmp_path / "old.sqlite"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE proposals (
            proposal_id TEXT PRIMARY KEY,
            valid_until_ts REAL NOT NULL,
            estimated_usd REAL NOT NULL,
            created_ts REAL NOT NULL
        );
        INSERT INTO proposals VALUES ('old-id', 0, 0, 0);
        """
    )
    conn.commit()
    conn.close()

    # Opening the store triggers the migration; the new schema must be usable.
    store = ProposalStore(db)
    cols = {
        r["name"] for r in store.conn.execute("PRAGMA table_info(proposals)").fetchall()
    }
    assert {"ticker", "side", "qty", "price", "dry_run"} <= cols
    # The stale pre-params row was dropped.
    assert store.get_proposal("old-id") is None
