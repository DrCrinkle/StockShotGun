# ADR 0006: Execution Engine as Core — Router Is the Engine, CLI/TUI/MCP Are Adapters

- **Status**: Proposed (Draft — 2026-05-31)
- **Amends**: ADR 0003 (reframes "Router as agent surface" → "Router as execution core")
- **Relates**: ADR 0004 (registry), ADR 0005 (self-describing proposal)

## Context

ADR 0003 introduced the `Router` as "the agent-facing surface," with the CLI/TUI
described as the "legacy path." The migration off `order_processor` was then done
by **bolting the Router onto the side of `main.py`** through a glue module,
`agentic/cli_bridge.py`, rather than by making the CLI a first-class client of the
Router. The result is two execution paths that have silently drifted apart.

### There are three callers of the engine today

| Caller | Entry | How it proposes | How it executes |
|---|---|---|---|
| Operator CLI | `agentic/cli.py` | `router.propose_order(...)` | `router.execute_order(...)` |
| MCP / agent | `agentic/router/__main__.py` → FastMCP tools | `router.propose_order` | `router.execute_order` |
| **Main CLI + TUI** | `main.py`, `tui/app.py` → `cli_bridge` | **`gate_order(...)` directly** | `router.execute_order` via `execute_via_router` |

The first two are clean clients and prove the engine's public API is already
sufficient. The third reimplements the propose half by hand — and gets it wrong.

### The drift is concrete, not cosmetic

`Router.propose_order` (`router/_server.py:700`) discovers real per-account targets
and binds `dry_run` into the hashed intent:

```python
accounts_by_broker = await self._discover_accounts(names)   # real account_ids per broker
intent = self._build_intent(..., dry_run=dry_run)           # caller's dry_run is authoritative
```

`cli_bridge.apply_main_py_gate` (`cli_bridge.py:84`) does neither:

```python
targets = tuple(BrokerAccount(broker, "primary") for broker in brokers_to_use)  # one fake leg
intent  = OrderIntent(..., dry_run=False)                                       # always live
```

Consequences of the hand-rolled path:

1. **ADR 0001 (per-account positions) is bypassed on the main CLI + TUI.** They
   only ever fan out to a single synthetic `"primary"` account per broker. A
   broker with a taxable + IRA account gets one leg, not two — the exact thing
   ADR 0001 exists to prevent. The agent path does it right; the human path does
   not.
2. **`--dry-run` never reaches the gate or router.** `run_trade` short-circuits
   `--dry-run` to a broker-*readiness* preflight (`cli/trade.py:93`) and returns
   before `apply_main_py_gate` is ever called. A dry run therefore reports
   "READY / NOT READY" per broker but exercises *none* of the enforcement
   pipeline — no limit check, freeze list, reconciliation, or token minting. It
   builds confidence about credentials, not about whether the order would be
   allowed. The live path then hardcodes `dry_run=False` on both propose
   (`cli/trade.py:182`) and execute (`:235`), so there is no way to rehearse a real
   order through the full pipeline from the main CLI at all. *(Verified
   2026-06-01: `buy --dry-run --mock-brokers` returns a readiness report, not a
   gate result — the "dry_run_mismatch" I suspected from reading was a false alarm.
   The propose/execute `dry_run` flags match, so no mismatch occurs.)*
3. **The result shape is a lie by translation.** `execute_via_router` reshapes the
   engine's native `{results:[{broker, ok, reason}], ...}` back into the retired
   `order_processor` dict `{successful, failed, skipped, statuses}` so old callers
   "don't need refactoring." Every adapter now speaks a dead format.

`cli_bridge.py` is migration scaffolding (its own docstrings say "v0.3", "v0.4
closure", "legacy", "deferred"). It was never meant to be load-bearing, but it is:
**the agentic layer is now mandatory for the non-agentic CLI, via the worst of the
three doors.**

## Decision

Promote the Router to a **neutral execution engine** that the CLI, TUI, operator
CLI, and MCP server all sit on top of as thin adapters. Delete `cli_bridge`. There
is one propose path, one execute path, one result type.

### 1. Move + rename: `Router` → `ExecutionEngine`, out of `agentic/`

```
src/agentic/router/_server.py  →  src/execution/engine.py   (class ExecutionEngine)
```

`agentic/` keeps a re-export (`Router = ExecutionEngine`) during migration so
nothing breaks on day one. The name stops advertising "agent only" — the engine is
the core, agents are one client.

### 2. Separate the broker *runtime* from the MCP *transport*

`BrokerMCPServer` (`agentic/_base.py`) is misnamed: it is not an MCP server, it is a
broker-call executor (gate → rate-limit → SDK → audit). Only the FastMCP tool
registration and `SubprocessBrokerProxy`'s stdio are genuinely "MCP." Split on a
protocol so the engine never imports `agentic/`:

```
src/execution/ports.py        BrokerPort  (Protocol: place_at_broker,
                                            get_holdings_at_broker,
                                            list_accounts_at_broker, health_check)
src/execution/in_process.py   InProcessBroker   (today's BrokerMCPServer, renamed)
src/agentic/subprocess.py     SubprocessBrokerProxy  (BrokerPort over MCP stdio — stays)
```

The engine depends on `BrokerPort`, not on MCP. In-process vs subprocess (ADR 0003)
becomes a wiring choice behind one protocol — the "no two trust models" goal, now
also "no two import graphs."

### 3. One public API, one result type

The engine already exposes everything the adapters need:

```python
class ExecutionEngine:
    @classmethod
    def from_brokers(cls, *, isolation="in_process") -> "ExecutionEngine": ...
    async def propose_order(*, ticker, qty, side, brokers=None, price=None,
                            dry_run=True) -> Proposal
    async def execute_order(*, proposal_id, dry_run=False) -> ExecutionResult
    async def place_order(...)  -> ExecutionResult       # one-shot propose+execute
    async def get_holdings(...) / run_sweep(...) / sell_arrived(...) / list_brokers()
    async def validate_targets(...)                      # ← preflight_validate moves here
```

`ExecutionResult` (typed, replaces the `order_processor` dict) is the engine's
native return. Each adapter *renders* it:

- **CLI** → `cli_runtime.build_response_envelope` + `compute_trade_exit_code`
- **TUI** → widgets
- **MCP** → FastMCP tool response
- **Operator CLI** → JSON/text

`compute_trade_exit_code` reads `ExecutionResult` directly; no legacy reshaping.

### Before / After

```
BEFORE — two propose paths, glue in the middle

  main.py ─┐                              agentic/cli.py ─┐
  tui/app ─┴─► cli_bridge.py                              ├─► Router.propose_order
              ├─ apply_main_py_gate ─► gate_order(...)    │   (real accounts, dry_run bound)
              │     [BrokerAccount("primary"), live]      │
              ├─ execute_via_router ─► Router.execute_order ◄┘
              └─ reshape → {successful, failed, skipped}     FastMCP tools ─► Router.*
                                                  │
                              Router lives in agentic/, imports BrokerMCPServer (also agentic/)


AFTER — one engine, thin adapters, MCP isolated to the edge

  cli/trade.py ─┐
  tui/app.py    ├─► execution/ExecutionEngine.{propose_order, execute_order, place_order}
  agentic/cli   ┤        │  (real account discovery + dry_run binding for EVERYONE)
  agentic/router┘        ▼
   (FastMCP)        enforcement/ (gate, propose/execute, audit)
                         ▼
                  BrokerPort  ──in_process──► InProcessBroker   (execution/)
                              └─subprocess──► SubprocessBrokerProxy (agentic/, MCP stdio)
                         ▼
                  registry → broker SDKs

  import direction:  agentic/ ──► execution/ ──► enforcement/   (never the reverse)
  cli_bridge.py: deleted
```

## Migration (incremental, each step ships green)

0. **Characterize.** Lock current `main.py buy/sell` and TUI output with golden
   tests (`--mock-brokers`), including the per-account count and `--dry-run`
   behavior, so the drift fixes are visible as intentional diffs.
1. **Move + alias.** ✅ *Done 2026-06-01.* Class `Router` → `ExecutionEngine` in
   `agentic/router/_server.py` with a module-level `Router = ExecutionEngine`
   alias; `agentic/router/__init__.py` exports both names; new canonical home
   `execution/` (`__init__.py` + `engine.py`) re-exports the engine so downstream
   can `from execution import ExecutionEngine`. The class body stays in `agentic/`
   for now (the `execution/` re-export points back into `agentic/` — a documented,
   temporary inversion that step 2 flips). Full suite + golden tests unchanged
   (213 passed).
2. **Protocolize brokers.** ✅ *Done 2026-06-01.* Added `execution/ports.py`
   (`BrokerPort` Protocol, `runtime_checkable`); moved the broker runtime to
   `execution/in_process.py` with `BrokerMCPServer` → `InProcessBroker` (+ alias);
   moved cross-cutting observability to `execution/telemetry.py`. `agentic/_base.py`
   keeps the FastMCP/stdio *transport* and re-exports the runtime for back-compat;
   `agentic/_telemetry.py` is a re-export shim. The engine now imports brokers +
   telemetry from `execution/` and annotates its map `dict[str, BrokerPort]`.
   Verified at runtime: `InProcessBroker` *and* `SubprocessBrokerProxy` both
   satisfy `BrokerPort`. New `tests/test_execution_layering.py` locks that
   `execution/{in_process,ports,telemetry}.py` import no `agentic/`. Full suite
   214 passed (was 213) + golden tests unchanged. *Remaining inversion:*
   `execution/engine.py` still re-exports the engine body from
   `agentic/router/_server.py` — relocating that body is the next step and
   completes the direction flip (then it joins the layering test).
3. **Repoint the main CLI.** Rewrite `cli/trade.py` to call
   `engine.propose_order` + `engine.execute_order` (the calls `agentic/cli.py`
   already makes). Drop the direct `gate_order` call. *Main CLI now gets real
   per-account discovery for free* (fixes Context #1), and `--dry-run` can become a
   true full-pipeline rehearsal — `propose_order(dry_run=True)` +
   `execute_order(dry_run=True)` runs limits/freeze/reconciliation without placing
   orders, replacing the credentials-only readiness check (fixes Context #2). Both
   are visible, intentional diffs to golden-test in step 0.
4. **Repoint TUI + batch + automate.** Same substitution in `tui/app.py`,
   `cli/batch.py`, `cli/automate.py`.
5. **Delete `cli_bridge.py`.** Replace its result reshaping with a
   `render_execution_result()` in `cli/common.py`. `preflight_validate` becomes
   `engine.validate_targets`.
6. **Lock the layering.** Add a test asserting no module under `execution/` or
   `enforcement/` imports `agentic/`.

## Consequences

**Positive**
- One propose path → the per-account (ADR 0001) and `dry_run` (ADR 0005)
  guarantees hold for *every* caller, not just agents. Two latent bugs die.
- `cli_bridge` and the last `order_processor`-shaped dict are deleted.
- `agentic/` becomes a true adapter: FastMCP tools + subprocess transport, nothing
  else. The engine is testable without any MCP machinery.
- In-process / subprocess is one wiring switch behind `BrokerPort`.

**Negative / risks**
- **Behavior change is user-visible.** The main CLI will fan out to all discovered
  accounts instead of one `"primary"` leg — order counts change. This is more
  correct but must be announced and golden-tested (step 0). For a single-account-
  per-broker setup it's a no-op; for taxable+IRA it doubles legs.
- **Churn across the CLI/TUI.** Mechanical but broad. Mitigated by the alias in
  step 1 keeping every import valid until each caller is repointed.
- **`place_order` one-shot vs explicit two-step.** Keep both: interactive CLI may
  want propose → show estimate → confirm → execute; scripts/agents want one call.

## Reversibility

High. Steps 1–2 are pure renames/moves behind an alias. If step 3+ proves too
invasive, the alias lets the old `cli_bridge` path coexist while only some callers
migrate. No data migration — proposals remain ephemeral (ADR 0005).

## Open questions

- Should `--dry-run` route through the engine as a real dry proposal (full-pipeline
  rehearsal), or do operators rely on today's fast credentials-only readiness
  check? Possibly keep both (`--dry-run` = readiness, `--rehearse` = full pipeline).
- Package name: `execution/` vs `core/`. `execution/` reads as a sibling of
  `enforcement/` (both neutral libs); `core/` risks becoming a junk drawer.
- Should `run_sweep` / `sell_arrived` stay on the engine, or move to an
  `rsa/` service that *uses* the engine? They're orchestration over execution, not
  execution itself — arguably a fourth adapter, not engine surface.
