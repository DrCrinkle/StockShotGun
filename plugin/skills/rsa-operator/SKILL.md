---
name: rsa-operator
description: >-
  Drives the reverse-split-arbitrage (RSA) lifecycle over the ssg-router MCP with
  human-gated buys and sells: scan the Nasdaq split calendar for signals, verify that
  fractional shares round up at the beneficial-owner level, evaluate survivors against
  configured thresholds, stage and execute approved 1-share buys across enabled brokers,
  then sweep and sell post-split positions. Every buy and every sell requires the
  operator's explicit per-item approval in session. USE WHEN rsa, reverse split, rsa
  review, review signals, rsa sweep, sell arrived, rsa status, check the splits calendar.
  NOT FOR general trading chat, single-broker queries against a broker's own MCP, or
  StockShotGun development work.
license: MIT
compatibility: Requires Python 3.14+, uv, a populated .env with broker credentials, and the ssg-router MCP from this plugin
metadata:
  repository: "https://github.com/DrCrinkle/StockShotGun"
  version: "0.1.0"
---

# RSA Operator

Operator for the agent-operated reverse-split-arbitrage engine
(`specs/agent-operated-rsa-engine.md`). This skill drives **real brokerage accounts**
through the `ssg-router` MCP. It is Phase 1: human-gated. Nothing buys or sells without
the operator's explicit per-item "yes" in the current session.

The skill never uses the `automate` due-buy path. Buys happen directly via
`propose_order` / `execute_order`.

## The boundary

The application is hands and ledger. This skill is the brain.

- **The skill decides**: whether a split's terms actually round up fractional shares for
  a beneficial owner, whether a signal clears the thresholds, which broker set to fan out
  to, when a swept position is ready to sell, and when to stop.
- **The code enforces**: dollar limits, the corporate-action freeze list, pre-buy
  reconciliation, the circuit breaker, confirmation-token TTL and intent binding,
  per-leg idempotency, and the tamper-evident audit log. Those gates run in
  `src/enforcement/` on every path and cannot be talked out of a rejection.

Never reimplement an enforcement gate in prose, and never work around one. A gate
rejection is a stop, not an obstacle.

## Workflow routing

| Workflow | Trigger | File |
|----------|---------|------|
| **Review** | "rsa review", "review signals", "run the rsa play", "check the splits calendar" | `references/review.md` |
| **Sweep** | "rsa sweep", "sell arrived", "sweep the trade" | `references/sweep.md` |
| **Status** | "rsa status", "how are my rsa plays doing" | `references/status.md` |

Supporting references, loaded on demand:

| Reference | When to read it |
|-----------|-----------------|
| `references/fractional-treatment.md` | Before assigning a verdict to any calendar signal. Required reading in Review step 4. |
| `references/broker-settlement.md` | When judging whether a swept position is late or merely inside its clearing tier's normal window. |

## Configuration

Thresholds live in the file named by the `RSA_PREFERENCES` environment variable,
defaulting to `${PLUGIN_DATA}/preferences.md`. Keys:

- `min_ratio` (e.g. `1:5`)
- `per_play_cap_usd`
- `max_share_price_usd`
- `min_days_to_effective`
- `enabled_brokers` (`all` or an explicit list)

**If the preferences file does not exist, stop and say so. Do not stage a live buy.**
Real money has no safe default threshold, and inventing one is the single worst failure
mode available to this skill.

**If the resolved store path is not absolute, stop and say so.** The router reads
`SSG_DB_PATH`; when it is unset the store path is relative and the process working
directory decides which database a real-money write lands in. Check it before the first
state-changing call, not after.

## Gotchas

- **Dismiss, never promote, signals this skill acts on.** After a buy executes, close the
  originating calendar signal with
  `dismiss_signal(signal_id, reason="bought <date> trade #<id>")` — NEVER
  `promote_signal`. Promoting feeds the `automate` due-buy queue, which would fire the
  same buy again. `promote_signal` exists only for the separate recap-driven flow this
  skill does not use.
- **`record_rsa_trade` is mandatory after every live buy, and it refuses in several
  cases — check `trade_id`, not just `ok`.** `run_sweep` / `sell_arrived` only ever see
  trades that exist as `rsa_trades` / `rsa_positions` rows, and nothing else creates
  those rows for an agent-driven buy. Skipping this call after a real `execute_order`
  leaves the play unsweepable and unsellable forever. Three distinct refusals, not
  interchangeable:
  - `execution["dry_run"]` is true, or `execution["results"]` has zero `ok=True` legs:
    refuses, writes nothing, no `trade_id`. Safety feature. Don't call it at all in
    these cases.
  - **Duplicate guard**: the call matches an already-recorded OPEN trade (same
    ticker + ratio + legs). Refuses with `ok:false` but **includes the existing
    `trade_id`**. This is the exact path hit when retrying after a timeout, and the
    trade IS already tracked. Treat as success: use that `trade_id` and proceed to
    dismiss. Do NOT alarm, do NOT insert a row by hand — that creates a duplicate in
    the live trading database.
  - **Ticker mismatch or other unexpected refusal**: `ok:false` with NO `trade_id`.
    This is the genuine untracked-live-buy problem. Surface it loudly.
- **NULL `effective_date` is dangerous, not benign.** A calendar signal with no
  effective date is treated as immediately due if acted on. Flag it loudly in the review
  table and never recommend it as the pick. Verify the real split date from a secondary
  source, or wait for the calendar to update, before staging a buy.
- **`dry_run=true` is a full-pipeline rehearsal, not a no-op.** Gates run for real and
  proposals mint for real; nothing places at a broker. Never call `record_rsa_trade`
  from a rehearsal, and never narrate a rehearsal as if it were a trade.
- **The account-scoped dispatch guard fails real account ids on account-blind brokers.**
  12 of 13 broker trade functions are account-blind (`multi_account=False`); only Fennel
  accepts a real `account_id`. A leg addressed to a non-`"primary"` account id on any
  other broker is refused with `reason="account_scoped_dispatch_unsupported"`. Surface
  it, don't retry with a different account id, and don't treat it as transient.
- **Approved is not executed.** Every `execute_order` call needs its own fresh explicit
  "yes" in the current session. An earlier approval, even for the same ticker, does not
  carry forward. A proposal whose TTL expired means re-proposing, not reusing the token.
- **Never operate on the live store with synthetic or test data.** Rehearsals, fixtures,
  and drills point `--db-path` (or `SSG_DB_PATH`) at a copy.

## Examples

**Morning review**

```
User: "rsa review"
→ scan_signals(refresh=true)
→ assign a fractional-treatment verdict to each new signal (references/fractional-treatment.md)
→ evaluate survivors against RSA_PREFERENCES thresholds
→ present ONE table: every signal, its verdict, the filing language behind it, and one recommended pick
→ operator approves one candidate → propose_order → estimate shown → second "yes" → execute_order
→ record_rsa_trade + dismiss_signal, summary reported
```

**Post-split sweep**

```
User: "sweep the KAPA trade, check for arrived shares"
→ run_sweep(trade_id, dry_run=false)
→ sell_arrived(trade_id) for share_arrived legs → proposal shown → "yes" → execute_order
→ summary per broker against its own clearing-tier expectations
```

**Quick check**

```
User: "rsa status"
→ src/main.py status --output json (no MCP call, no state change)
→ compact digest: open trades, positions by state, signal counts
```
