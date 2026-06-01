# ADR 0005: Self-Describing Proposal — Persist Order Params, Delete the Router Intent Cache

- **Status**: Accepted
- **Date**: 2026-05-31
- **Amends**: ADR 0003 (resolves the "Router intent cache" wrinkle it deferred to v0.5)

## Context

A `Proposal` stored only intent *hashes* and per-leg metadata — not the
concrete order parameters (`ticker`, `side`, `qty`, `price`, `dry_run`). But
`execute_order` needs those concrete values to call each broker's
`place_at_broker`. ADR 0003 bridged the gap with a router-side sidecar,
`_router_intent_cache: {proposal_id → {ticker, side, qty, price}}`, populated at
propose time and read at execute time — with TTL cleanup explicitly deferred.

That sidecar leaked enforcement state into the router and gave the Proposal and
its params different lifetimes and storage:

- A router restart between propose and execute → cache miss
  (`proposal_intent_uncached`) even though the durable Proposal row survived.
- Propose on one Router instance / process and execute on another → miss.
- The legacy `cli_bridge.apply_main_py_gate` path had to poke the router's
  private cache directly (`router._router_intent_cache[...] = ...`) because it
  calls `gate_order` rather than `Router.propose_order`.
- No TTL cleanup → unbounded growth.

The key realization: **the cache was never a trust boundary.** Each leg's
`intent_hash` is the security authority — `place_at_broker` reconstructs a
single-target `OrderIntent` from the params it's handed, hashes it, and
`consume_leg` rejects the leg unless that hash equals the stored `intent_hash`.
Wrong params → wrong hash → rejected before any SDK call. So the params can live
anywhere without weakening safety; the cache was merely *where to replay from*.

## Decision

Make the `Proposal` **self-describing**: persist `ticker`, `side`, `qty`,
`price`, and `dry_run` on the `proposals` row, and delete the router intent
cache entirely.

- `Proposal` (`enforcement/types.py`) gains the five fields. `propose_fanout`
  sets them from the gated `OrderIntent`; `ProposalStore.insert` / `get_proposal`
  round-trip them (`price` nullable → `None` for market orders).
- `execute_order` reads the params from the Proposal returned by
  `get_proposal` — no in-memory state. It therefore works against a fresh
  Router instance / process that never saw the propose call, as long as it
  shares the durable `ProposalStore`.
- **`dry_run` is enforced, not free.** It is part of the hashed intent, so a
  proposal is born live-or-dry. `execute_order` rejects a caller whose `dry_run`
  disagrees with the proposal's (`reason: "dry_run_mismatch"`) instead of
  silently flipping the order's mode or failing every leg with an opaque
  `intent_mismatch`.
- **Invariant guard.** `propose_fanout` asserts that the master params reproduce
  *every* leg's `intent_hash`. True by construction today (all legs derive from
  one intent), but a future change that varies params per leg — making a single
  stored param set a lie for some legs — now fails loudly rather than persisting
  the wrong `qty`/`price`. Heterogeneous quantities are already split into
  separate proposals (`sell_arrived`, ADR 0003), so one param row per proposal
  is correct.
- `_router_intent_cache` is removed: the field, both write sites
  (`propose_order`, the `sell_arrived`/`place_order` fan-out loop), the
  `cli_bridge` write, and the `execute_order` read + `proposal_intent_uncached`
  miss path. No fallback — keeping one would signal distrust of the durable
  store.

### Schema migration

The `proposals` table gains `ticker TEXT NOT NULL, side TEXT NOT NULL,
qty REAL NOT NULL, price REAL, dry_run INTEGER NOT NULL`. The existing
detect-and-drop migration (`_migrate_pre_f2c_schema`) is extended to also drop a
`proposals` table lacking the new columns. Proposals are 300s-ephemeral, so
dropping in-flight rows on a shape change is acceptable — the established pattern
here.

## Consequences

**Positive**

- The Proposal round-trips through its own store; execute is sufficient from
  `proposal_id` alone, across restarts and across processes (in-process router
  and subprocess broker proxies share one reconstruction path — ADR 0003's
  "no two trust models" goal).
- Removes a fragility seam (cache miss → `proposal_intent_uncached`) and an
  unbounded, un-cleaned in-memory dict.
- `cli_bridge` no longer reaches into router private state.
- `dry_run` flips are an explicit, tested rejection rather than an opaque
  per-leg failure.

**Negative**

- Order params now persist in SQLite for the proposal's 300s TTL. They are
  non-sensitive (`ticker/qty/side/price` — ISC-16 sanitizes credential-shaped
  *keys*, none of which these are). Proposal-row cleanup remains a separate
  concern (rows linger past TTL until a future cleanup job; same as before —
  this change does not add a cleanup obligation, it moves the data from RAM to
  the row that already lingered).

## Reversibility

Reversible by reintroducing a cache and dropping the columns, with no data
migration (proposals are ephemeral). The forward direction is the path of least
regret: the durable store becomes the single place the order's identity lives.

## Implementation

| Piece | Source | Tests |
|-------|--------|-------|
| Params on Proposal + store | `src/enforcement/types.py`, `src/enforcement/propose_execute.py` | `tests/enforcement/test_proposal_params.py` |
| Invariant guard | `propose_execute.propose_fanout` | `tests/enforcement/test_proposal_params.py::test_stored_params_reproduce_every_leg_hash` |
| execute reads store + dry_run enforce; cache deleted | `src/agentic/router/_server.py`, `src/agentic/cli_bridge.py` | `tests/agentic/test_self_describing_proposal.py` |
