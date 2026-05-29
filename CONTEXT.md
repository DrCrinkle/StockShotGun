# StockShotGun — Domain Context

StockShotGun is a general-purpose multi-broker trading tool that submits orders to
multiple brokerage accounts simultaneously. Reverse split arbitrage (RSA) is a primary
use case but not the only one.

---

## Glossary

### Trade
A simultaneous buy (or sell) of the same ticker across multiple brokers at one point
in time. Not inherently an RSA event — any coordinated multi-broker order is a Trade.

### RSA Trade
A Trade explicitly declared at buy time as a reverse split arbitrage event. Declared
fields: `split_ratio` (e.g., `"1:25"`) and `expected_split_date`. RSA intent must be
stated when the Trade is created; it cannot be assigned retroactively. Created by
running `buy --rsa --ratio N:D [--expected-date YYYY-MM-DD]`. The `rsa_trades` row is
written immediately on buy success; Positions are not — see Lazy Position Discovery.

### Position
One broker-account's holding within a Trade. Granularity is per-account, not
per-broker, because a single broker may have multiple accounts (e.g., taxable + IRA)
that behave differently during a split. Fields: `broker`, `account_id`, `pre_split_qty`.

When buying into a Trade, all accounts at each broker are included. Each account
becomes a separate Position.

### pre_split_qty
The number of shares held at a specific broker-account before the reverse split
processes. Discovered at the first sweep run from observed holdings (which are still
pre-split at that point) — see Lazy Position Discovery. Not entered manually at sweep
time.

### Lazy Position Discovery
Positions are not written at buy time. The `buy --rsa` flow only writes the
`rsa_trades` row. The first `sweep --from-trade <id>` run queries holdings on every
enabled broker, and for each `(broker, account_id)` pair that reports the ticker with
a non-zero quantity, lazy-creates an `rsa_positions` row with
`pre_split_qty = observed_qty`. The same sweep run then classifies status using that
just-recorded pre_split_qty.

Why deferred: the buy command does not capture per-account fill data (broker
`Trade()` functions return broker-level success/failure only), and a post-buy
holdings query would race with broker settlement. By the time the first sweep runs
(typically hours-to-days post-buy), holdings have settled and account_ids are
authoritative.

Subsequent sweep runs use the recorded pre_split_qty unchanged — observed_qty drift
between sweeps is interpreted via `SweepStatus`, not by re-fixing pre_split_qty.

### expected_split_date
A mutable estimate of when the reverse split takes effect. Stored on the RSA Trade,
starts as the announced date, updated when confirmed. Used with a broker's
`processing_window_days` to resolve ambiguous sweep states.

### Sweep
The act of querying all broker APIs to determine the current status of each Position
in an RSA Trade. Runs once per day (cron or manual invocation) to avoid overloading
broker APIs. Each run's result is appended to the full history — not just the latest
snapshot. Status changes are reported to stdout.

### SweepStatus
Derived at query time by comparing observed broker holdings against `pre_split_qty`
and `expected_post_qty`. Never stored as authoritative — always recomputed live, then
written to history.

| Status | Meaning |
|---|---|
| `AWAITING_SPLIT` | Broker still shows pre-split quantity; split hasn't processed here yet |
| `PROCESSING` | No shares visible; broker is mid-settlement |
| `FRACTIONAL_PENDING` | Fractional share delivered; waiting for broker round-up |
| `SHARE_ARRIVED` | Post-split share is visible and ready to sell |
| `AMBIGUOUS` | Observed qty equals both pre-split qty and expected post-split qty; cannot distinguish states |
| `SKIPPED` | No credentials configured for this broker |
| `ERROR` | API query failed |

### AMBIGUOUS
Occurs when `observed_qty == pre_split_qty == expected_post_qty`. Practically: if you
bought 1 share in a 1:25 split, `expected_post_qty = ceil(1/25) = 1`. You can't tell
from quantity alone whether the share is pre- or post-split.

Resolution (both apply):
1. **Date math**: if `today > expected_split_date + processing_window_days`, treat as
   `SHARE_ARRIVED` automatically.
2. **`--force` flag**: explicit override for when the date is wrong or unknown.

### expected_post_qty
`ceil(pre_split_qty × ratio_numerator / ratio_denominator)`. Computed, never stored.

### BROKER_PROFILES
Per-broker settlement characteristics used by sweep to generate status details and
resolve ambiguity. Key fields:

| Field | Meaning |
|---|---|
| `clearing` | Clearing firm (`apex`, `self`, `rqd`, `unknown`) |
| `processing_window_days` | Days after effective date until post-split shares typically appear |
| `fractional_intermediate` | Broker delivers fractional first, then rounds up (Robinhood, TastyTrade) |
| `round_up_expected` | Broker rounds fractional post-split shares up to 1 whole share |
| `trade_may_be_blocked` | Broker may block selling until split processing completes |
| `cil_likely` | Broker pays Cash-in-Lieu instead of rounding up (Schwab, Chase) |

### CIL (Cash-in-Lieu)
When a broker holds a fractional post-split share, it may pay the cash equivalent
rather than rounding up to a whole share. CIL brokers (Schwab, Chase) are still
included in Trades because StockShotGun is a general-purpose tool — CIL positions
still return a profit, just smaller and less reliable than a round-up.

### D-suffix Ticker
After a reverse split, some brokers temporarily show positions under `{ticker}D`
(e.g., `XYZD`) rather than the original symbol. The sweep logic falls back to querying
the D-suffix ticker when no position is found under the primary ticker. The exact
semantics (whether `D` represents old or new shares) vary by broker and are treated
as approximate.

### Round-up
When a broker holds a fractional post-split share (e.g., 0.04 shares after a 1:25
split), it rounds up to 1 whole share. The profit from RSA comes from selling this
rounded-up share at market price.

### Round (RSA community term)
The Nth time a specific stock has done a reverse split. "Round 2" means this is the
second RSA opportunity on that ticker. Informational only — stored in buy signal
`notes`, not as a DB field. Community members track it because prior round outcomes
(CIL vs round-up) inform expectations for the current round.

---

## Signal System (automation_recap.py)

StockShotGun has a signal system that parses structured **Recaps** from the RSA
community forum (reversesplitarbitrage.com) into actionable buy and sell signals.
This workflow is not yet wired to automatic execution — buys are currently initiated
manually via TUI or CLI.

### Recap
Structured text published periodically in the RSA forum chat. Contains several
sections parsed by `automation_recap.py`:

| Section | Parsed? | Purpose |
|---|---|---|
| **Stocks Back and Latest** | Yes | Crowd-sourced broker settlement observations |
| **Upcoming Buys** | Yes | Dated RSA opportunities → buy signals |
| **TBA** | Yes (watchlist) | Undated opportunities; activated when date assigned |
| **Research Posted** | Notes only | Community analysis stored on buy signal; does not gate execution |
| **Chatter** | No | Free-form community discussion; too noisy to parse reliably |
| **Notices** | No | Forum housekeeping |

### Buy Signal
A pending buy action derived from an Upcoming Buys entry. Fields: `ticker`,
`target_date` (null for TBA), `ratio`, `notes` (includes round number, prior
outcomes, research notes). Status: `watchlist` → `pending` → `executed` | `cancelled`.

Due signals (those with `target_date` matching today or earlier) are presented for
user confirmation before execution. A `--yes` flag bypasses confirmation once the
parser is trusted.

### Watchlist
State for TBA buy signals — stored but never surfaced as due. Promoted to `pending`
when a future recap assigns a date to the ticker.

### Sell Trigger
Created when a **Stocks Back** entry is new or its details change. Signals that a
position at a specific broker may need to be sold.

### Stocks Back
Community members reporting which broker their post-split share arrived at, and the
outcome (round-up vs CIL). Example: `VIVK(D) - Tasty` means TastyTrade returned the
share under the D-suffix ticker. This is crowd-sourced settlement data — a future
improvement could use it to calibrate `BROKER_PROFILES.processing_window_days` from
real observations rather than hardcoded estimates.

The D-suffix appears in the community's own vocabulary (e.g., `VIVK(D)`), not just
internally in the sweep code.

---

## Ticker Rules (planned, see `specs/ticker-rules-workshop.md`)

Per-ticker automation controls so recap-driven actions are configurable without
changing code. Stored in a `ticker_rules` SQLite table keyed by ticker.

### Configurable per ticker

| Field | Purpose |
|---|---|
| `enabled` | Master switch; if false, skip all automation for ticker |
| `buy_qty` | Override the global default buy quantity |
| `sell_qty_mode` | `holdings` (live-derived) or `fixed` |
| `sell_qty_fixed` | Used when `sell_qty_mode = fixed` |
| `buy_brokers_json` | Restrict buy to listed brokers |
| `sell_brokers_json` | Restrict sell to listed brokers |
| `allow_buy` / `allow_sell` | Side-specific gating |
| `notes` | Free-form annotation |

### Behavior precedence

1. **CLI flags hard-override**: `--broker` ignores `buy_brokers_json` / `sell_brokers_json` entirely (not intersection).
2. `ticker_rules` values when present.
3. Existing automation defaults.

### Sell quantity capping

When `sell_qty_mode = fixed` and `sell_qty_fixed` exceeds current holdings, cap at
holdings automatically — never sell more than you hold, regardless of the rule value.

### Management interface

Rules are managed via a CLI command (`automate-rules list|get|set|delete`), not raw
SQL. Auditable, doesn't require schema knowledge.

### Effective date windows

Out of scope for first pass. Rules persist until explicitly updated or deleted. Add
later only if found necessary.

### Auditability

Every generated order includes rule context in automation JSON output:
`rule_applied`, `rule_fields_applied`, `rule_source`.

---

## Position Lifecycle (RSA)

```
[Buy] → AWAITING_SPLIT → PROCESSING → FRACTIONAL_PENDING → SHARE_ARRIVED → [Manual Sell]
                    ↘ AMBIGUOUS (resolved by date math or --force) ↗
```

Each day sweep runs, it appends a timestamped result row to the position history.
The lifecycle is derived from history, not stored as a state machine field.

---

## Key Design Decisions

- **RSA declared at buy time**: intent cannot be assigned retroactively.
- **Per-account positions**: one Position per broker-account pair, not per broker.
- **pre_split_qty discovered lazily at first sweep**: from observed_qty, before split processes. Not re-entered at sweep time. Order results are not currently captured per-account; lazy discovery sidesteps that. See ADR 0002.
- **Full sweep history**: every poll result stored; latest snapshot also derivable.
- **Once-per-day polling**: cron or manual; daemon is aspirational.
- **Notification**: stdout log only (for now).
- **AMBIGUOUS resolution**: date math first, `--force` as escape hatch.
- **Sell timing**: per-broker as each share arrives, not all-at-once when all brokers settle.
- **Sell order type**: market or limit depending on broker; `BROKER_PROFILES` should eventually encode preferred sell order type per broker. Convention: `price=0` (falsy) → market order; `price=N` → limit order at N. Consistent across all broker implementations.
- **TBA signals**: stored as watchlist, never surfaced as due until a date is assigned.
- **Research Posted**: stored as notes on buy signal; does not gate execution.
- **Recap ingestion**: confirmation required before executing due signals; `--yes` bypasses.
- **Ticker rules CLI override**: `--broker` hard-overrides ticker broker lists, not intersection.
- **Sell qty cap**: `sell_qty_fixed` caps at current holdings automatically.
- **Ticker rules management**: CLI command, not raw SQL.
