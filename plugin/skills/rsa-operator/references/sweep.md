# Sweep

**Goal:** For trades past their expected split date, find shares that have arrived
post-split and sell them, with an explicit human "yes" before any live sell.

## Step 1 — Snapshot open trades

Two ways in:

- **Broad check:** run `src/main.py status --output json` with the repo's virtualenv
  interpreter, from the repo root. Gives every open trade, its positions, and signal
  counts in one shot.
- **Single known trade:** `get_rsa_trade(trade_id)` via MCP. Read-only; returns the
  `rsa_trades` row plus all `rsa_positions` rows for that id.

Either way, identify every trade whose `expected_split_date` is on or before today and
which still has positions not yet in a terminal sold state.

## Step 2 — Sweep each due trade

```
run_sweep(trade_id=<id>, dry_run=false)
```

`dry_run=false` classifies every position against live broker holdings AND writes the
classification to `sweep_state`. This is a real state-changing call even though it never
places an order.

Per-position `resolved_status` values: `share_arrived`, `processing`, `ambiguous`,
`awaiting_split`, `fractional_pending`, `error`.

## Step 3 — Judge each position against its own clearing tier

Read `references/broker-settlement.md` for what "normal" looks like per tier, and
`BROKER_PROFILES` in `src/sweep.py` for the live numeric windows. Do not hardcode
timings into your report; read the file if precision matters.

Present each position's `resolved_status` next to what is expected for **its own
broker's tier**. An Apex-cleared broker showing 0 shares three weeks after the split is
behaving normally; a self-clearing broker doing the same is stuck. Flag anything late
relative to its own tier, never relative to the fastest broker.

## Step 4 — Sell arrived legs

For every leg classified `share_arrived`:

```
sell_arrived(trade_id=<id>, price=<optional limit price>)
```

This internally re-sweeps (dry-run) and proposes a sell for every arrived leg, grouped by
observed quantity, so it can return more than one `proposal_id` when arrived quantities
differ across brokers.

Present ALL returned proposals, including any `ok:false` groups with their
`reason` / `detail`. Do not hide gate rejections. **Get an explicit "yes" per proposal**
before executing; a multi-group return means multiple separate approvals, not one blanket
yes.

For each approved proposal:

```
execute_order(proposal_id=<id>, dry_run=false)
```

Report per-leg results (`success_count` / `failure_count`, plus reason and detail for any
failed leg).

## Step 5 — Account-scoped dispatch guard

If any leg fails with `reason="account_scoped_dispatch_unsupported"`, the position carries
a real (non-`"primary"`) `account_id` on a broker whose trade function is account-blind,
which is every broker except Fennel today. Surface this as a structural limitation and do
not retry. Retrying with a different account id or broker list will not help; the
underlying `TradeFn` needs threading for `account_id` first, which is out of scope here.

## Step 6 — Summary

Report: trades swept, positions by resolved status, sells proposed / executed / failed,
and any structural blocks hit in Step 5.
