# ADR 0002: Lazy Position Discovery — First Sweep Creates rsa_positions

- **Status**: Accepted
- **Date**: 2026-05-04

## Context

For RSA tracking we need per-`(broker, account_id)` `Position` rows so that
`sweep --from-trade <id>` can classify post-split holdings against
`pre_split_qty`. The question is **when** those rows get written, given
the data available at each point.

Four candidate moments / mechanisms were evaluated:

1. **B1 — Threaded fill data through `OrderBatchProcessor`.** Modify all 12
   broker `Trade()` functions to return per-account `[{account_id, filled_qty}]`
   instead of broker-level success/failure. Buy time would then have everything
   needed.

2. **B2 — Post-buy holdings query.** Immediately after `process_orders`
   returns, query `holdings(ticker)` per successful broker, parse out
   `(account_id, observed_qty)` per account.

3. **B3 — Session-cached `account_ids` + `args.quantity`.** Read account ids
   already cached in `session_manager.sessions[broker]["account_ids"]` (set
   during init for Webull, Fennel, Public, Tradier; uneven coverage). Pair
   with the quantity the user asked for as `pre_split_qty`. Use a placeholder
   `account_id=""` when session has no list.

4. **B4 — Defer Position rows to first sweep.** Buy time writes only the
   `rsa_trades` row. The first `sweep --from-trade <id>` discovers
   `(broker, account_id, pre_split_qty=observed_qty)` from real holdings and
   lazy-creates `rsa_positions` rows.

## Decision

**B4.** `buy --rsa` writes `rsa_trades` only. Position rows are lazy-created
on first `sweep --from-trade <id>` from observed pre-split holdings. Zero-qty
observations do not create rows; the trade row persists and a later sweep can
catch up.

## Alternatives Considered

### B1 — Threaded fill data (rejected)

Most data-faithful option. Rejected because it requires changing the return
contract of every broker `Trade()` implementation in `src/brokers/*.py` — a
12-module surface area expansion that vastly outscopes the slice and risks
regressions in the most sensitive code path (real money submission).

### B2 — Post-buy holdings query (rejected)

Reuses existing infrastructure with no broker contract changes. Rejected for
three reasons:

1. **Settlement race.** A market buy submitted seconds ago may not yet be
   reflected in `holdings()` for some brokers; partial fills compound this.
2. **Pre-existing position confusion.** If the user already had a non-RSA
   position in the same ticker, `holdings()` cannot distinguish it from the
   just-bought RSA position.
3. **Limit orders.** Unfilled or partially-filled limit orders aren't in
   holdings, so the recorded `pre_split_qty` would be wrong or zero.

### B3 — Session account_ids + args.quantity (rejected)

Avoids settlement race. Rejected because:

1. **Heterogeneous coverage** across brokers: only some cache `account_ids`
   in their session payload. Brokers without it would fall back to a
   placeholder, producing two distinct write paths.
2. **`args.quantity` ≠ filled quantity** for partial fills or limit orders,
   so `pre_split_qty` would be optimistic.
3. **Placeholder `account_id=""` rows** would be orphaned by sweep, since
   sweep's results are keyed by real account_ids — defeating the whole point.

## Consequences

**Positive**:

- No broker contract changes.
- No settlement race — first sweep typically runs hours-to-days post-buy.
- `pre_split_qty` reflects the actual settled holding, not a request quantity
  that may diverge from the fill.
- Real `account_id` values come from real holdings data — no placeholders.
- Buy command stays minimal: one INSERT into `rsa_trades`, no broker-results
  parsing.

**Negative**:

- `sweep --from-trade <id>` semantics change from slice 1: it must now permit
  "trade exists, no positions yet" on first invocation and lazy-create rows.
  `load_trade_for_sweep` (in `src/sweep_persistence.py`) needs to allow empty
  `position_rows` instead of erroring; the discovery+create step gets added
  to the sweep flow.
- A buy that does not get followed by a sweep run leaves an "empty" trade
  with no positions. This is acceptable but worth surfacing in CLI output
  ("no positions discovered yet — run `sweep --from-trade <id>`").
- If a broker's holdings are still empty at first sweep (e.g., delayed
  settlement or buy quietly failed despite OrderBatchProcessor reporting
  success), no position is recorded for that broker. A later sweep run can
  pick it up. This is a feature, not a bug — we don't want phantom positions.

**Neutral**:

- Subsequent sweep runs use the recorded `pre_split_qty` unchanged. Drift in
  `observed_qty` between sweeps is interpreted via `SweepStatus`, not by
  re-fixing `pre_split_qty`.

## Reversibility

Reversing to B1 (threaded fill data) is possible but expensive — requires
broker contract changes. Reversing to B2/B3 means populating positions
earlier; existing trade rows would already have positions from past
lazy-discovery and would coexist fine with the new mechanism — so the
reverse direction is non-destructive but means "we already wrote positions,
this newer mechanism just writes them sooner." Forward switch (B4 ⇒ B1) is
the path of least regret.
