# RSA Agent Workflow

> How an MCP-aware agent drives StockShotGun's reverse-split arbitrage loop end-to-end. This doc names the decision/code boundary, the sell-trigger heuristic, broker settlement window semantics, partial-failure semantics, the persistence contract, and the loop termination conditions.

## The loop

```
                        ┌──────────────────────────┐
                        │   buy decision (agent)   │
                        │  (ticker + ratio + ETA)  │
                        └────────────┬─────────────┘
                                     │
                          ┌──────────▼──────────┐
                          │  propose_order      │
                          │  (router MCP, gate) │
                          └──────────┬──────────┘
                                     │ proposal_id
                          ┌──────────▼──────────┐
                          │  PRINCIPAL APPROVAL │  ← humans gate live orders
                          └──────────┬──────────┘
                                     │ approved
                          ┌──────────▼──────────┐
                          │  execute_order      │
                          │  (per-leg tokens)   │
                          └──────────┬──────────┘
                                     │ rsa_trades row + rsa_positions
                          ┌──────────▼──────────┐
                          │  WAIT (split date)  │
                          └──────────┬──────────┘
                                     │
                ┌────────────────────▼────────────────────┐
                │  run_sweep(trade_id) (read-only, daily) │
                │  classifies each position per broker    │
                └────────────────────┬────────────────────┘
                                     │
                ┌────────────────────▼────────────────────┐
                │  agent reviews classifications          │
                │  ARRIVED legs → propose sell            │
                │  AMBIGUOUS legs → wait or escalate      │
                │  AWAITING_SPLIT → wait                  │
                │  ERROR → surface to principal           │
                └────────────────────┬────────────────────┘
                                     │ ARRIVED present
                          ┌──────────▼──────────┐
                          │  propose_order sell │
                          │  + principal gate   │
                          └──────────┬──────────┘
                                     │
                          ┌──────────▼──────────┐
                          │  execute_order sell │
                          └─────────────────────┘
```

## The decision/code boundary

| The agent decides | The code enforces |
|-------------------|-------------------|
| Which RSA opportunity to enter (ticker, ratio, ETA) | Dollar limits per order + per day (ISC-13/14) |
| Which brokers to fan out across | Per-symbol freeze list (ISC-42) |
| When to sweep (cadence — daily by default) | Intent-binding hash on every proposal (ISC-39) |
| When a sweep result is "ready enough" to propose a sell | Per-leg single-use tokens (ISC-11/12/40) |
| Whether to escalate AMBIGUOUS classifications early | Circuit breaker on consecutive broker errors (ISC-43) |
| When to terminate the loop (timing, drawdown, fatigue) | Tamper-evident audit log of every propose + execute (ISC-45) |
| Whether to retry a partial-failure broker | Settled-cash + PDT pre-check skip-list (ISC-44) |

**The agent never bypasses code.** If it tries to execute without a valid proposal_id, the router returns `rejected: true, reason: proposal_not_found`. If it tries to execute with a stale or tampered token, the broker leg returns `reason: intent_mismatch` or `reason: token_already_used`. If the dollar limit would be breached, `propose_order` raises before any token is minted.

## The sell-trigger heuristic

For each per-broker, per-account `Position` in an RSA trade, `run_sweep` returns a `resolved_status`. The agent's sell-trigger rule:

1. **`share_arrived`** — sell. The broker's reported holdings have settled to the post-split quantity. This is the canonical "ready" signal.
2. **`ambiguous`** — wait OR escalate. Observed quantity equals both `pre_split_qty` and `expected_post_qty`, which means either (a) the split was 1:1, or (b) the broker hasn't updated holdings yet. The `resolve_ambiguous_with_date` step in the router upgrades AMBIGUOUS → SHARE_ARRIVED when `today > expected_split_date + processing_window_days` for that broker's profile. If the per-broker window hasn't elapsed, the agent waits.
3. **`awaiting_split`** — wait. Observed quantity equals `pre_split_qty` but `pre_split_qty != expected_post_qty`. The split has not processed yet. No action.
4. **`fractional_pending`** — wait. The broker is mid-roundup for sub-share quantities. Most brokers settle these within 1-3 trading days.
5. **`processing`** — wait. Observed quantity is 0 or in flux. The broker is mid-settlement.
6. **`error`** — surface to principal. The broker MCP couldn't return holdings (auth expired, rate-limit, SDK exception).

Per-broker `processing_window_days` lives in `src/sweep.py::BROKER_PROFILES`. Default for unknown brokers is 20 days.

## Per-broker failure semantics during sweep

`run_sweep` fans out `get_holdings_at_broker` calls across every broker that has a position in the trade. **Per-broker failures isolate.** One broker's auth-expired or SDK-explosion does NOT halt classification of the other brokers' positions. The failing broker's positions get `error: <message>` in their classification entry; the summary count includes them under `error`. The agent decides whether to:

- proceed with the sells for the brokers that did report (typical)
- pause the whole loop and surface the failing broker to the principal (when a critical broker is down)

The router does not make this call — it surfaces structured per-broker results and lets the agent (or the principal) decide.

## Persistence contract

The agent reads RSA state through three tables, all in `logs/automation.sqlite3` (overridable via `Router.rsa_store_path`):

| Table | Owner | What the agent reads |
|-------|-------|----------------------|
| `rsa_trades` | `rsa_store.RsaStore.create_trade` writes; agent reads via `get_rsa_trade` | `ticker`, `split_ratio`, `expected_split_date`, `created_at` |
| `rsa_positions` | Lazy-discovered on first sweep (see ADR 0002); agent reads via `get_rsa_trade` | `broker`, `account_id`, `pre_split_qty` |
| `sweep_state` | `rsa_store.RsaStore.record_sweep` writes per sweep; agent reads via `get_rsa_trade` | `status`, `observed_qty`, `expected_post_qty`, `last_checked`, `sold_at` |

The agent never writes these tables directly. `run_sweep` is read-only in v0.1 — the legacy `python3 main.py sweep --from-trade <id>` flow is what writes `sweep_state` today. v0.2 of `run_sweep` will optionally write the state through the router after classification.

## Termination conditions

An RSA agent loop **must** name explicit termination conditions in the agent's own prompt. The infrastructure provides primitives; the agent is responsible for using them.

| Condition | What stops the loop |
|-----------|---------------------|
| **Max iterations** | Stop after N sweep cycles even if positions remain. Suggested default: 30 (≈1 month at daily cadence). |
| **Max wall-clock** | Stop after T hours of agent execution time, independent of iteration count. |
| **Max sell spend** | Stop if cumulative sell-side dollars exceed a configured cap (mirrors per-day buy limit). |
| **Principal halt** | Any non-OK response to the principal's confirmation prompts halts the loop. |
| **Circuit breaker open** | If `list_brokers` shows N+ brokers with `breaker_open: true`, the agent halts and escalates. |
| **All positions terminal** | When every position has `resolved_status` in `{share_arrived (sold), sold}`, the loop is complete. |

Agents that run unbounded against a live broker layer are the classic blowup. Don't ship an RSA loop without explicit termination conditions.

## The two-step gate (non-negotiable)

Every order — buy AND sell, every iteration of the loop — goes through the two-step propose/execute flow:

1. **propose_order** — agent supplies intent, router runs full enforcement pipeline (freeze, limits, circuit, settled cash, reconciliation), audit log gets a `propose` entry, per-leg tokens get minted. Returns `proposal_id`.
2. **principal confirmation** — agent surfaces the proposal_id + estimated_usd + skipped_brokers to the principal. Live execution requires explicit `--live` from a human.
3. **execute_order** — agent calls with `proposal_id`. Router looks up per-leg tokens; each broker leg validates its own token against its single-target intent and places the order. Per-leg outcomes return in the response; partial failure is first-class.

**There is no single-call live order path.** `place_order` with `dry_run=False` and no `proposal_id` is explicitly rejected (ISC-18).

## What's not in this doc

- **How to choose which RSA opportunities to enter** — that's the agent's edge, not the infrastructure's. The router gives you the primitives; the agent provides the signal.
- **Tax accounting** — out of scope. Use a downstream tool (or the audit log) to reconstruct cost basis.
- **Cross-trade coordination** — each RSA trade is independent. Multiple agents driving multiple trades simultaneously are supported by the per-leg-token design but not orchestrated here.

## Versions

- **v0.1 (current):** `get_rsa_trade` + `run_sweep` (read-only) + the two-step propose/execute flow for buy AND sell. Sweep classification is the documented agent surface.
- **v0.2 (planned):** `run_sweep(dry_run=False)` writes `sweep_state` through the router, mirroring what `python3 main.py sweep --from-trade <id>` does today.
- **v0.3 (planned):** sell-on-arrived helper tool that takes a `trade_id`, runs `run_sweep`, and auto-proposes sells for every ARRIVED leg in a single MCP call (still requires explicit `--live` for execute).
