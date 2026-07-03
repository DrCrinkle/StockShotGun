# Spec: Agent-Operated RSA Engine

- **Status**: Approved design (2026-07-01)
- **Relates**: ADR 0003 (MCP fanout), ADR 0004 (broker registry), ADR 0006 (Router as execution core)
- **Supersedes**: nothing — this is additive; the CLI/TUI human path is untouched

## Task Description

Turn StockShotGun from a human-operated multi-broker order tool into an agent-operated reverse-split-arbitrage engine. An AI agent runs the full RSA lifecycle — detect upcoming reverse splits, buy 1 share across all brokers, track the split, sweep for rounded-up shares, sell them — using the existing Router/MCP surface, with a human approval gate on buys until the system earns auto-execution.

Explicitly **not** in scope: a web interface (rejected — personal-only deployment, agent-operated; the human surface is for auditing, served by Pulse), multi-user support, latency-critical execution, and fixing the ADR 0006 `cli_bridge` drift (already tracked separately; the agent path uses the Router directly and is unaffected).

## Objective

A PAI scheduled routine runs 1–2x daily. The agent scans for reverse-split signals, evaluates candidates, and stages buy proposals across all configured brokers. Taylor gets a notification with the candidate summary and approves or rejects. Approved plays execute through the Router's enforcement pipeline. Post-split, the agent sweeps and auto-sells arrived shares. Status is visible in Pulse. After a clean track record, a config flag enables auto-execution of buys under enforcement limits.

## Problem Statement

The execution half of RSA already works: the Router exposes `propose_order` / `execute_order` / `run_sweep` / `sell_arrived` over MCP, the enforcement pipeline gates every order, and the sweep + persistence layers shipped in April–May 2026. What's missing is everything around it:

1. **No detection.** Nothing finds reverse-split announcements. `rsa_trades.signal_id` exists in the schema but references nothing.
2. **No playbook.** An agent handed the MCP tools has no encoded knowledge of the RSA lifecycle, per-broker settlement behavior, or when each tool applies.
3. **No scheduling or approval flow.** Nothing runs the loop unattended, and there is no human-gated path from "candidate found" to "order executed."
4. **No observability surface.** Play status lives in SQLite and terminal output only.

## Architecture

StockShotGun remains a standalone deterministic engine per ADR 0006. The Router is the core; the enforcement pipeline is the hard gate; all adapters sit on top:

```
                      ┌─ CLI / TUI  (human, unchanged)
  signals ─► Router ──┼─ MCP "ssg-router"  (agent — existing tools + scan_signals)
  (new)     + gate    └─ Pulse feed  (observability, new)

  PAI side: skill (playbook) + scheduled routine + notification/approval
```

Two new layers in this repo (signals, Pulse feed), two on the PAI side (skill, routine). No changes to broker modules, registry, enforcement, or the Router's execution path.

## Component 1: Detection layer (`signals`)

The main build. Deterministic code, no LLM involvement.

- New `src/signals/` module + CLI command `python3 src/main.py signals scan`.
- Polls free reverse-split announcement sources. **v1: Nasdaq corporate-actions calendar.** v2 (later): SEC filings (8-K / DEF 14A) as a confirming second source.
- Normalizes hits into a new `calendar_signals` table in `AutomationRecapStore` (`automation_recap.py`, `logs/automation.sqlite3`) — alongside the existing signal tiers (`buy_signals`, `research_signals`, `tba_candidates`) that `recap_ingest` already populates. Columns: ticker, ratio (N:D), effective date, source, raw payload, signal_key (dedup), status (`new`, `promoted`, `dismissed`, `expired`), first/last seen.
- Worthwhile signals are **promoted into the existing `buy_signals` queue**, which the `automate` due-buy path already consumes — the calendar becomes a second signal source next to chat recaps, not a parallel pipeline.
- Idempotent: re-scanning updates `last_seen`, never duplicates. A signal whose effective date passes without action expires automatically.
- Output is JSON (machine-readable) with a human table via the normal CLI formatting.
- Exposed as a new MCP tool `scan_signals(refresh: bool)` on `ssg-router`: `refresh=true` polls sources; `refresh=false` reads staged signals from the store. Also `dismiss_signal(signal_id, reason)` so the agent can mark rejects.

**Testing:** parser unit tests against fixture payloads captured from the real source (including malformed/edge rows); store tests for idempotency and expiry; no network in tests.

## Component 2: Agent playbook (PAI skill)

A PAI skill (`~/.claude/skills/RsaTrader/`) encoding the lifecycle:

1. **Scan** — `scan_signals(refresh=true)`; for each `new` signal, evaluate.
2. **Evaluate** — price sanity (1 share cheap enough per enforcement limits), ratio worth playing (configurable minimum, default 1:5 — higher denominators mean a bigger round-up gain), effective date far enough out to settle buys, ticker tradable across brokers. Dismiss with reason if not.
3. **Stage** — `propose_order` for 1 share buy across all enabled brokers/accounts; notify Taylor with candidate summary + proposal IDs; **stop** (Phase 1).
4. **Execute on approval** — `execute_order` for approved proposals; buy path persists `rsa_trades`/`rsa_positions` (existing behavior — see memory: RSA capture at buy time).
5. **Track** — after effective date, `run_sweep(trade_id)` on the routine's cadence, respecting per-broker processing windows from `BROKER_PROFILES` (self-clearing ~days, Apex-cleared ~weeks; fractional-first brokers like Robinhood need the intermediate state handled, not sold).
6. **Sell** — `sell_arrived` for brokers where the post-split share landed. Sells may auto-execute from the start (bounded downside; delay costs money).
7. **Report** — outcome summary per play; P&L when closed.

The skill embeds the per-broker settlement-tier knowledge (clearing firm, processing window, round-up vs cash-in-lieu behavior, trade-blocked windows) so the agent sets expectations per broker instead of treating all 13 alike. Source of truth stays `BROKER_PROFILES` in `src/sweep.py`; the skill references it rather than duplicating values.

## Component 3: Scheduling + approval flow

- **PAI scheduled routine**, 1–2x daily, invokes the skill. RSA timing is hours-to-days, so episodic runs suffice — no daemon.
- **Phase 1 (launch): human-gated buys.** Agent stages proposals and notifies (PAI notification with ticker, ratio, effective date, total cost, per-broker leg count). Execution only on Taylor's explicit approval in a follow-up interaction. Proposals expire if unapproved before the effective-date cutoff.
- **Phase 2 (earned): auto-execute.** A config flag (env var in `.env`, read by the skill) flips buys to auto-execute under enforcement limits: per-order dollar cap, per-play total cap, freeze list authoritative. Flag ships **off**; flipping it is a deliberate manual act after a clean Phase 1 track record.
- **Kill switch:** disable the routine + freeze list. Both existing mechanisms.

## Component 4: Pulse feed

Small status emitter pushing to the local Pulse instance (`localhost:31337`): open trades, positions by sweep status, staged proposals awaiting approval, recent executions, realized P&L per play. Read-only, derived entirely from the SQLite store: originally specced as `stockshotgun status --json`; shipped as `python3 main.py status` (JSON emission goes through main.py's global `--output json` flag, not a per-command `--json` flag — see the CLI envelope deviation in `specs/rsa-signals-detection-plan.md`) that a Pulse module polls. As shipped, the snapshot covers open trades + positions by sweep status and signal-queue counts (`calendar_signals`, `buy_signals`, `pending_sell_triggers`) — staged-proposal detail, recent-execution history, and realized P&L per play are not yet in the snapshot and remain future work. Polling over push keeps the repo free of any Pulse dependency — StockShotGun exposes JSON, PAI consumes it.

## Safety invariants

1. Every agent-initiated order goes through `propose_order` → gate → `execute_order`. No side paths. (The gate-before-execute guard tests already enforce this ordering.)
2. The enforcement pipeline (limits, freeze list, reconciliation, token minting) is authoritative and unchanged.
3. `dry_run` binds into the hashed intent (existing Router behavior) — rehearsals cannot be silently promoted to live orders.
4. Buys are human-gated until the auto-execute flag is deliberately flipped; the flag defaults off.
5. Detection is deterministic code; the LLM judges *whether* to play a signal, never *what the signal data is*.

## Testing strategy

- **Signals:** fixture-based parser tests, store idempotency/expiry tests (see Component 1).
- **MCP tools:** `scan_signals` / `dismiss_signal` covered by the existing router test pattern.
- **Integration:** full lifecycle rehearsal with `--mock-brokers` + `dry_run=true` — scan (fixture source) → evaluate → propose → execute → sweep → sell — before any live order.
- **Skill:** rehearsal run against the dry-run stack from a real agent session; verify notification content and approval gating.
- Run with `.venv/bin/python` (system python lacks `h2`).

## Build order

1. `signals` module + `rsa_signals` table + CLI command — ✅ shipped (feat/rsa-signals-detection)
2. `scan_signals` / `dismiss_signal` MCP tools — ✅ shipped (feat/rsa-signals-detection) — also shipped `promote_signal`, not originally itemized here but part of the same detection layer (see Component 1)
3. PAI skill (playbook) — not started
4. Scheduled routine + notification/approval flow (Phase 1) — not started
5. Pulse status feed — ✅ shipped (feat/rsa-signals-detection) — the repo-side half only: `python3 main.py status` now emits the aggregate JSON snapshot described in Component 4. The Pulse-side module that polls it (PAI repo, not this one) is not started.
6. (Later, earned) auto-execute flag — Phase 2 — not started

Each step is independently shippable and useful: after step 1 the signals scan is a useful human tool on its own; after step 4 the system is live in human-gated mode.

Remaining work (items 3, 4, 6) is PAI-side or config-only: the `RsaTrader` skill
and scheduled routine consuming the MCP tools shipped in items 1/2/5, plus the
auto-execute flag once Phase 1 earns a track record. See Plan 2 (not yet
written, per the scope note in `specs/rsa-signals-detection-plan.md`).
