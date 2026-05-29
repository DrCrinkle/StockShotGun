# ADR 0003: MCP Fan-Out Architecture — Per-Broker MCPs + Router + Per-Leg Tokens

- **Status**: Accepted
- **Date**: 2026-05-27
- **Supersedes**: None (extends ADRs 0001 + 0002 which cover per-account positions and lazy discovery)

## Context

StockShotGun started as a Python CLI/TUI for submitting orders to many brokers
simultaneously. In May 2026, Robinhood shipped an official MCP server for
agentic trading. That re-framed the project's value proposition: an agent
talking to Robinhood's MCP gets one broker; an agent talking to StockShotGun
gets one call that fans out across thirteen. The coordination work this
project already did is the differentiator in the agentic era.

The question this ADR answers is: **what shape should the agentic interface
take?** Three plausible designs:

1. **One monolithic MCP server.** Single Python process exposing one fan-out
   `place_order` tool that calls every broker SDK internally.
2. **Thirteen per-broker MCP servers + an aggregator/router MCP.** Each broker
   runs as its own MCP server process; a thin router fans out by calling each
   one. The agent's surface is the router; the per-broker MCPs are
   implementation detail.
3. **Thirteen per-broker MCP servers, no router.** Agent talks to each broker
   directly and does its own fan-out. (Same as Robinhood's model, scaled up.)

A second, deeply-coupled question: **how should confirmation tokens work?**
The first-cut design minted ONE token per fan-out, bound to a multi-target
intent hash. That works in-process but breaks across process boundaries (the
broker subprocess sees a single-target intent; its hash never matches; the
single-use token can only be consumed by one leg anyway).

## Decision

**Adopt design #2** — per-broker MCPs + a thin aggregator router — paired with
a **per-leg-token model** where each leg of a fan-out has its own
single-use token bound to its own single-target intent hash.

### Per-broker MCP servers

Each of the 13 brokers (Robinhood, Tradier, TastyTrade, Public, Firstrade,
Fennel, Schwab, BBAE, DSPAC, SoFi, Webull, Wells Fargo, Chase) runs as
`python -m agentic.brokers.<name>` — its own MCP server process with its own:

- credentials (env vars scoped to the process)
- rate limiter (per-broker limits from `brokers.base.RateLimiter`)
- circuit-breaker state (consecutive errors, last-error timestamp)
- audit-log writer
- session state cache

Each per-broker MCP exposes 4 tools: `place_at_broker`,
`get_holdings_at_broker`, `list_accounts_at_broker`, `health_check`. The
tool surface is identical for every broker; the implementation differs.

### Router MCP

`python -m agentic.router` is the **agent-facing surface**. It exposes 9
fan-out tools (`list_brokers`, `get_holdings`, `propose_order`,
`execute_order`, `place_order`, `get_rsa_trade`, `run_sweep`, `sell_arrived`,
`recap_ingest`). The agent never talks to a per-broker MCP directly in the
documented contract — though nothing prevents it for debugging.

The router holds either:

- **In-process broker servers** (v0.2 default) — `BrokerMCPServer` instances
  share the router's Python process. Fastest, simplest, suitable for
  single-machine deployments.
- **Subprocess broker proxies** (`SubprocessBrokerProxy`) — each broker
  runs as a child process; the router speaks MCP-stdio to each. True
  blast-radius isolation: one broker crash stays in that broker's process.

### Per-leg tokens

`propose_order` mints **N tokens, one per (broker, account_id) target**.
Each leg token is bound to a SHA-256 hash of a **single-target** `OrderIntent`
(`targets=(this_leg,)`). The agent receives a `proposal_id` (the master
record); the router internally maps it to the per-leg tokens at execute time.

When the router fans out `execute_order`, each broker leg gets its OWN token
and validates it via `BrokerMCPServer.place_at_broker` against its own
single-target intent. Trust model:

- Validation happens at the broker boundary, not in the router
- Single-use is enforced per leg, so partial failure (one leg's
  `token_already_used` or `intent_mismatch`) does NOT halt sibling legs
- The same `place_at_broker` MCP tool works for in-process AND subprocess
  callers — no escape-hatch method, no two trust models

## Consequences

### Positive

- **Differentiator is in the right place.** The agent gets one fan-out tool;
  the coordination logic is the value, the per-broker integrations are the
  isolation boundary.
- **Blast radius isolation.** A flaky broker (auth refresh bug, network
  partition, SDK exception) affects only that broker's process. The other 12
  keep running.
- **Credential isolation.** Each per-broker MCP process only sees the env
  vars for its broker. A credential compromise scopes to one broker.
- **Per-broker observability.** Logs, rate-limit state, circuit-breaker state
  all per-process. Operators debug one broker at a time.
- **Partial-failure first-class.** Per-leg tokens mean per-leg outcomes —
  one Fennel intent_mismatch doesn't block the Tradier leg.
- **Subprocess + in-process modes share the same code path.** No two trust
  models; `BrokerMCPServer.place_at_broker` validates its leg token
  identically regardless of caller.

### Negative

- **More processes to manage** in subprocess mode (14 instead of 1). Mitigated
  by the in-process default and per-broker `python -m` entrypoints that any
  process manager can supervise.
- **Schema-migration burden** when the proposal store shape changes. Pre-F2c
  sqlite files need migration; we handle this with an auto-detect-and-drop
  step in `ProposalStore._migrate_pre_f2c_schema` (proposals are short-lived
  — 300s TTL — so dropping is acceptable).
- **Heterogeneous arrived quantities** in `sell_arrived` produce one proposal
  per qty group rather than one master proposal — slight UX wrinkle that
  the agent has to iterate over `proposals: list` instead of a single id.
- **Router intent cache** — `execute_order` needs the per-leg call args
  (ticker, qty, side, price); the Proposal record stores only hashes. We
  cache the args at propose time in a router-side `_router_intent_cache`.
  TTL-driven cleanup is v0.5 work.

### Rejected alternatives

**One monolithic MCP server.** Rejected because:
- Crash blast radius is the entire process
- One credential file contains everything (high-value target)
- Per-broker SDK upgrades risk breaking unrelated brokers
- Per-broker observability gets buried in one log stream

**Thirteen per-broker MCPs, no router.** Rejected because:
- Pushes the coordination problem back onto the agent — exactly what this
  project was built to solve
- Two-step propose/execute would have to be repeated 13 times per fan-out
- The audit log would be 13 disjoint files

**Single fan-out token.** Rejected (after the F2 build surfaced the bug):
- Token binds to multi-target intent hash, broker sees single-target intent →
  hash mismatch
- Token is single-use → only the first leg can ever consume it
- Cannot cross process boundaries cleanly — subprocess brokers can't share
  the validation state

## Implementation

| Feature | Source | Tests |
|---------|--------|-------|
| Enforcement core | `src/enforcement/` | `tests/enforcement/test_gate.py` |
| Per-broker MCPs | `src/agentic/brokers/<broker>/` | `tests/agentic/test_broker_mcp_server.py`, `tests/agentic/test_fastmcp_runtime.py` |
| Router MCP | `src/agentic/router/` | `tests/agentic/test_router.py` |
| Per-leg tokens | `src/enforcement/propose_execute.py` `propose_fanout` / `validate_leg_for_execute` | `tests/enforcement/test_gate.py`, `tests/agentic/test_router.py` |
| Subprocess proxy | `src/agentic/_subprocess.py` | `tests/agentic/test_subprocess.py` |
| Multi-account discovery | `src/agentic/_base.py` `session_manager_accounts` | `tests/agentic/test_multi_account_discovery.py` |
| Legacy CLI wiring | `src/agentic/cli_bridge.py` + `src/main.py` + `src/tui/app.py` | `tests/agentic/test_main_py_gating.py`, `tests/agentic/test_f5_v04_router_execution.py` |
| RSA workflow tools | `src/agentic/router/_server.py` `get_rsa_trade` / `run_sweep` / `sell_arrived` | `tests/agentic/test_rsa_router.py`, `tests/agentic/test_rsa_sweep_writes.py` |
| Recap ingest | `src/automation_recap.py` + router `recap_ingest` | `tests/agentic/test_recap_ingest.py` |

The agent workflow that uses these primitives is documented at
[docs/agentic/RSA_AGENT.md](../agentic/RSA_AGENT.md).
