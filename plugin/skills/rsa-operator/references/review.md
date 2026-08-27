# Review

**Goal:** Turn today's scanned reverse-split signals into approved, executed, and tracked
buys, with two explicit human "yes" gates per trade (stage, then execute) and nothing left
dangling afterward (dismissed if skipped, recorded if bought).

## Step 1 — Load configuration

Read the preferences file named by `RSA_PREFERENCES` (default
`${PLUGIN_DATA}/preferences.md`). **If it does not exist, stop and say so.** Do not invent
default thresholds for a real-money workflow. Load `min_ratio`, `per_play_cap_usd`,
`max_share_price_usd`, `min_days_to_effective`, `enabled_brokers`.

Confirm the resolved store path is absolute (`SSG_DB_PATH`). If it is not, stop: a
relative path means the working directory decides which live database gets written.

## Step 2 — Scan

```
scan_signals(refresh=true)
```

This polls the Nasdaq calendar and stages fresh signals into the store. It never trades.
If the tool returns `{"ok": false, ...}` (calendar fetch or parse failure), report the
error plainly and stop. Do not fall back to `refresh=false` and present stale data as
fresh.

## Step 3 — Assign a fractional-treatment verdict

**Read `references/fractional-treatment.md` and apply it to every signal with
`status="new"` before any threshold arithmetic.** This is the gate the whole thesis rests
on: a reverse split only pays if the fractional share rounds **up at the beneficial-owner
level**, and most do not.

Work the stages in order, because stage 1 costs nothing:

1. **Structural rejects** — ETFs, series trusts, funds, ADRs and foreign ordinaries, and
   any signal whose effective date has already passed. No filing fetch needed.
2. **Read the filing** for everything that survives, and assign one of four verdicts:
   `round_up_beneficial`, `round_up_record_only`, `cash_in_lieu`, `unknown`.
3. **Record the language** the verdict rests on. Every verdict must be able to quote the
   sentence from the filing that produced it.

Only `round_up_beneficial` continues to Step 4. Everything else is carried into the table
with its verdict and dismissed in Step 8.

## Step 4 — Evaluate survivors against thresholds

For every signal that reached this step, compute:

1. **Ratio check** — parse the ratio (N:D). Reject if worse than `min_ratio`. Use the same
   comparison the codebase uses in `sweep.parse_ratio` / its ratio ordering; do not
   reinvent it.
2. **Cost check** — estimated play cost = share price × count of enabled-broker accounts
   (call `list_brokers()` if the account count is not already known this session). Reject
   if it exceeds `per_play_cap_usd`.
3. **Price check** — reject if the share price exceeds `max_share_price_usd`.
4. **Date check** — reject if the effective date is closer than `min_days_to_effective`
   days out.
5. **NULL effective_date — flag loudly, never recommend.** A signal with no effective date
   has immediately-due semantics if acted on later (see SKILL.md Gotchas). Mark it clearly
   (e.g. `⚠ NULL DATE`) and never make it the recommended pick even if every other check
   passes. Surface it so the operator can decide; the default posture is "verify the real
   date first."
6. **Broker honour check** — per `references/broker-settlement.md`, count how many enabled
   brokers are actually expected to honour a beneficial round-up. Schwab processes cash in
   lieu only, so its legs contribute cost without upside. Report the count; do not
   silently drop brokers.

Every signal ends with a verdict: **pass** (candidate), **fail: `<concrete reason>`**
(will be dismissed), or **flagged: NULL date** (shown, never recommended).

## Step 5 — Present one table

Present ALL evaluated signals in a single table: ticker | ratio | price | est. cost |
effective date | treatment verdict | filing language | brokers honouring | verdict.

Name exactly ONE recommended pick among the passing candidates (best ratio × lowest cost
× soonest safe date). **A signal whose treatment verdict is anything other than
`round_up_beneficial` can never be the recommended pick, regardless of ratio or cost.**
If nothing passes, say so plainly. Do not manufacture a pick.

## Step 6 — Per-item approval (first "yes")

For each signal the operator wants to act on, get an explicit "yes" per signal, not a
blanket "do all of them." Declined signals go to Step 8. Only proceed to Step 7 for
signals explicitly approved.

## Step 7 — Stage, then execute

```
propose_order(ticker=<ticker>, qty=1, side="buy", brokers=<enabled_brokers or None for all>, dry_run=false)
```

Present `estimated_usd`, `leg_count`, and `skipped_brokers` with reasons (insufficient
settled cash, PDT limit, and so on). This is informational, not a commitment.

**A second explicit "yes" is required before executing.** Staging and executing are two
separate gates; do not execute on the strength of the Step 6 approval.

```
execute_order(proposal_id=<id_from_propose>, dry_run=false)
```

Handle the result:

- **`GateError` raised during propose** — report the reason and stop that item. Do not
  retry with adjusted parameters without a fresh decision from the operator.
- **`rejected: true` in the execute result** (proposal not found, empty proposal, or
  `dry_run_mismatch`) — report plainly and stop. Never silently retry with a new proposal.
- **`success_count > 0`** — proceed to the recording step below.
- **Zero `ok=True` legs** — report the per-leg failures (broker, reason, detail) and stop.
  Do not call `record_rsa_trade`; it will refuse anyway.

### Recording the trade (MANDATORY)

```
record_rsa_trade(
  ticker=<ticker>,
  split_ratio=<ratio>,
  execution=<the exact dict execute_order returned>,
  expected_split_date=<effective_date>,
  signal_id=<signal_id>,
)
```

Branch on the result in this order:

1. **`ok: true`** — normal path. Take `trade_id` and use it in Step 8.
2. **`ok: false` AND the response includes a `trade_id`** — duplicate guard, not a
   failure. The trade was already recorded, which is the case hit when this sequence is
   retried after a timeout. Treat as success: take that `trade_id` and proceed to Step 8 as
   if `ok:true`. Do NOT alarm, and do NOT suggest or perform a manual `rsa_trades` insert;
   the row exists and inserting another creates a duplicate in the live database.
3. **`ok: false` AND no `trade_id`** (ticker mismatch, or any other unexpected refusal) —
   this is the genuine problem. Surface it loudly: a live buy happened and is not tracked.
   It needs manual follow-up. Never mention this in passing.

The presence of `trade_id`, not the `ok` flag alone, decides whether the trade is safely
tracked.

## Step 8 — Close out every signal

- **Bought** (execute succeeded and recording resolved to branch 1 or 2):
  `dismiss_signal(signal_id, reason="bought <today's date> trade #<trade_id>")`. Never
  `promote_signal`.
- **Rejected in Step 3** (treatment verdict): `dismiss_signal(signal_id, reason=<the
  verdict reason, e.g. "record-holder round-up only; street-name positions cash out">)`.
  Exception: leave `unknown` at `new` while the effective date is still in the future, so a
  later filing can be picked up.
- **Rejected in Step 4, or declined in Step 6**: `dismiss_signal(signal_id, reason=<the
  concrete reason, or "declined by operator">)`.

## Step 9 — Summary

Report: signals scanned, treatment verdicts assigned (with counts per verdict), how many
passed / failed / were flagged on thresholds, which were approved, execution outcomes per
ticker, and confirmation that every acted-on signal was dismissed or recorded.
