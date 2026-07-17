# ADR-0006 Completion Plan (Steps 3–6 + engine-body relocation)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish ADR 0006 — every surface (CLI, TUI, batch, automate) calls `ExecutionEngine.propose_order`/`execute_order` directly, `--dry-run` becomes a full-pipeline rehearsal, `cli_bridge.py` is deleted, and the engine body moves to `execution/engine.py` completing the import-direction flip.

**Architecture:** Steps 0–2 are done (golden tests, rename+alias, `BrokerPort` split). Remaining: a rendering helper translates the engine's native per-leg result into the legacy `{successful, failed, skipped, statuses}` shape each CLI/TUI consumer expects; `preflight_validate` moves onto the engine as `validate_targets` (per the ADR); the four callers repoint; the bridge and its tests die; the class body relocates; the layering test locks it.

**Tech Stack:** Python 3.13, pytest. Run tests with `.venv/bin/python -m pytest` (system python lacks h2). Baseline: **258 passed**.

**Decisions already made (do not relitigate):**
- `--dry-run` = **full-pipeline rehearsal** (`propose_order(dry_run=True)` + `execute_order(dry_run=True)`): limits, freeze list, reconciliation, token minting all run; no orders placed. The credentials-only readiness check (`_build_dry_run_readiness`) is retired from the dry-run path. (Principal decision, 2026-07-02.)
- `preflight_validate` becomes `ExecutionEngine.validate_targets` (ADR Decision §3).
- Per-account fan-out on the main CLI/TUI is an intentional, announced behavior change (ADR Consequences).
- `run_sweep`/`sell_arrived`/signal tools stay on the engine for now (ADR open question deferred).

**Key recon facts** (verify against code, they may drift):
- Engine `propose_order(*, ticker, qty, side, brokers=None, price=None, dry_run=True)` returns `{proposal_id, valid_until_ts, ttl_seconds, estimated_usd, brokers, accounts_by_broker, leg_count, ticker, side, qty, price, dry_run, skipped_brokers}` (src/agentic/router/_server.py ~795).
- Engine `execute_order(*, proposal_id, dry_run=False)` returns `{ticker, side, qty, dry_run, results: [{broker, account_id, ok, dry_run, idempotency_key, reason, detail}], success_count, failure_count}`; rejection adds `rejected=True, reason, detail` (~854).
- Legacy shape consumed by CLI printers, TUI widgets, and `cli_runtime.compute_trade_exit_code`: `{successful: int, failed: int, skipped: int, statuses: [{ticker, action, successful: [brokers], failed: [], skipped: []}]}`.
- Engine propose/execute audit through the enforcement core already — the `record_main_py_outcome*` bridge calls are NOT re-created on the new path; the enforcement audit trail is the canonical record (this is the ADR's "one propose path").
- cli_bridge callers: `cli/trade.py` (~lines 140–237), `cli/batch.py` (~160–280), `cli/automate.py` (~210–335), `tui/app.py` (`submit_all_orders` ~293, `retry_timed_out_brokers` ~405).

---

## Execution deviations (as built)

This plan shipped with a few departures from the task bodies below. The task
steps are left as originally written (they're still an accurate record of the
TDD process); this section is the correction layer.

- **Rejection-branch addendum on every caller.** The task bodies describe the
  happy path (propose → execute → render) but under-specify the rejection
  path. As built, `cli/trade.py`, `cli/batch.py`, and `cli/automate.py` all
  follow the same contract: a propose-time `GateError` and an execute-time
  `execution.get("rejected")` both raise `CliRuntimeError` with
  `ExitCode.FULL_BROKER_FAILURE` (or `gate_error_to_exit_code(gate_err)` for
  the propose-time case), rendering zeros (`successful=0, failed=0`) rather
  than reporting a rejection as any kind of success. `automate`'s rejection
  additionally carries `completed_results`/`completed_orders` in
  `details` so already-executed orders earlier in the same batch aren't
  silently dropped from JSON error output.
- **TUI rejection display parity, non-raising.** `tui/app.py` deliberately
  breaks from the CLI/batch/automate pattern above: it must never raise on a
  `rejected=True` execution (an uncaught exception would crash the TUI event
  loop mid-session). Both entry points (`submit_all_orders`,
  `retry_timed_out_brokers`) instead render the rejection as a `✗ Order
  rejected by enforcement gate (...)` status line, matching the wording used
  elsewhere, and continue running.
- **Two-phase propose-all-then-execute-all in `automate`, with incremental
  signal marking (mid-batch abort double-trade fix).** `cli/automate.py`
  proposes every generated order first (Phase 1) — the first `GateError`
  aborts the whole batch before anything executes, preserving
  `apply_main_py_gate_batch`'s original all-or-nothing propose semantics —
  then executes every gated proposal in order (Phase 2). Each order's
  signal(s) are marked executed *immediately* after that order completes,
  not in bulk after the full batch: a mid-batch abort on a later order must
  not leave an earlier, already-filled order's signal sitting in `'pending'`
  (it would re-execute — a double-trade — on the next `automate` run).
  Dry-run rehearsals place nothing anywhere and therefore never mark
  anything.
- **Circular-import fix: PEP 562 interim, structural fix in Task 7.** Early
  in the migration, `execution/engine.py` re-exported `ExecutionEngine` from
  `agentic/router/_server.py` (a documented, temporary inversion — see ADR
  0006 migration step 1). Task 7 relocated the class body itself into
  `execution/engine.py`, which is the structural fix: the interim
  re-export/shim is gone, and `agentic/router/_server.py` now imports
  `ExecutionEngine` from `execution.engine` (never the reverse), completing
  the import-direction flip the ADR calls for.
- **Dry-run rehearsal replacing readiness checks (trade/batch/automate).**
  All three surfaces retired their credentials-only `_build_dry_run_readiness`
  short-circuit; `--dry-run` now flows through the same
  `propose_order`/`execute_order` calls as a live order, both with
  `dry_run=True`, so limits/freeze/reconciliation/token-minting all run and
  nothing is placed. `_build_dry_run_readiness` has no remaining callers and
  was deleted.
- **`gate_error_to_exit_code` relocated to `cli/common.py`.** It originally
  lived in `agentic/cli_bridge.py`; all three live callers (`cli/trade.py`,
  `cli/batch.py`, `cli/automate.py`) now import it from `cli/common.py`,
  which survives `cli_bridge.py`'s deletion in Task 6.
- **Final-review C1 — account-blind trade fns x per-account legs = N² live
  orders.** `TradeFn(side, qty, ticker, price)` has no account parameter, so
  every broker's trade fn is account-blind; Fennel's additionally fans out
  internally over ALL session accounts (`brokers/fennel.py`). Fennel was the
  only `multi_account=True` spec, so per-account fan-out (this branch) gave
  it N legs, each triggering the internal N-account loop — 2 accounts = 4
  live orders. Two-part fix: (a) Fennel flipped to `multi_account=False`
  (verified: `build_broker_mcp_spec` maps the flag to the session-driven
  `make_session_accounts_fn` discovery closure vs `_default_single_account`,
  so the flip provably yields one `"primary"` leg — pinned by
  `test_fennel_spec_discovers_single_primary_leg`); (b) a structural guard
  in `InProcessBroker.place_at_broker`: a leg addressed to a real
  (non-`"primary"`) account id on a spec without the new
  `BrokerMCPSpec.account_scoped_trade` flag fails per leg with
  `reason="account_scoped_dispatch_unsupported"` before the SDK is touched
  (dry-run legs too, for rehearsal parity). The guard keys on the flag —
  never set by `build_broker_mcp_spec` while `TradeFn` is account-blind —
  rather than on `multi_account` or leg counts, because every real leg
  today is `"primary"` (all discovery fallbacks assign the placeholder), so
  the guard is provably inert for all current brokers and fires exactly
  when a future `multi_account=True` + blind-fn combination would
  double-buy. Tests that intentionally place legs at real account ids set
  `account_scoped_trade=True` on their fakes (simulating the future
  account-scoped broker) — each carries a justification comment.
- **Post-merge P1 (PR review) — Fennel's stopgap re-introduced the exact
  undercount C1 was meant to prevent, so it's fixed here.** C1's stopgap
  (`multi_account=False` on Fennel + the account-blind guard) closed the
  N² *live-order* multiplication, but left a subtler gap: `propose_order`
  still gated ONE "primary" leg while `fennelTrade` fanned out internally
  over every session account_id — so with N Fennel accounts, enforcement's
  estimate/leg-count/per-order-limit/daily-limit/audit all reflected 1
  order while N were actually placed. Every safety number was understated
  by N (not a double-buy, but a live-order that enforcement never gated at
  all). Fixed by completing the mechanism the guard was reserved for
  instead of leaving it inert:
  - `execution/in_process.py`: `place_at_broker` now calls
    `trade_fn(side, qty, ticker, price, account_id=account_id)` — the
    leg's own account — whenever `spec.account_scoped_trade` is True;
    blind specs (everyone else) are called exactly as before
    (`trade_fn(side, qty, ticker, price)`, no kwarg, no signature change
    for them).
  - `brokers/fennel.py`: `fennelTrade` gained an optional `account_id`
    kwarg. When given one (the engine's path, always, now), it places
    exactly ONE order via a new shared helper (`_fennel_submit_order`) —
    the internal loop over `account_ids` does NOT run on this path. When
    omitted (no caller does this anymore), the legacy blind fan-out loop
    still runs unchanged, calling the same shared helper per account —
    kept for back-compat only.
  - `brokers/registry.py`: Fennel flips back to `multi_account=True` (real
    per-account leg discovery via `make_session_accounts_fn`) AND gains
    `account_scoped_trade=True` — the two flags travel together now that
    the trade fn actually honors per-account dispatch. `BrokerSpec` gained
    the `account_scoped_trade` field (threaded through
    `build_broker_mcp_spec`, ADR 0004 single-source-of-truth) so this is a
    one-line registry change per broker as more migrate.
  - Net effect: `propose_order`'s leg_count/estimate and `execute_order`'s
    per-leg dispatch both reflect every real Fennel account, 1:1 with live
    orders. The guard from C1 is unchanged in behavior and still protects
    the other 12 (still account-blind) brokers.
  - Tests: `test_fennel_spec_discovers_single_primary_leg` (the C1-era
    golden) is superseded by its inverse,
    `test_fennel_spec_discovers_real_per_account_legs`
    (`tests/agentic/test_multi_account_discovery.py`) — 2 session
    account_ids now discover 2 real legs, not one `"primary"` placeholder.
    `test_registry_built_specs_are_never_account_scoped`
    (`tests/agentic/test_broker_mcp_server.py`) is superseded by
    `test_registry_built_specs_are_account_scoped_only_for_migrated_brokers`,
    which pins Fennel as the one exception rather than asserting none
    exist. New: `tests/test_fennel.py` exercises `fennelTrade` directly
    (session pre-seeded past `get_fennel_session`'s already-initialized
    branch, `http_client.post` monkeypatched) — the account-scoped call
    hits the SDK exactly once, for exactly the given account, never
    touching the other session accounts. New:
    `test_fennel_like_account_scoped_broker_enforcement_accounting_matches_live_orders`
    (`tests/agentic/test_router.py`) pins the P1 end to end: a 2-account
    account-scoped spec mints 2 gated legs with the estimate reflecting
    both, `execute_order` places exactly 2 orders (never 4 — no internal
    fan-out multiplication), and each trade-fn call receives its own
    `account_id`.
- **Final-review C2 — automate completion tracking compared rendered labels
  against bare broker names.** `completed_brokers_by_source` accumulated
  `status["successful"]` (rendered `_leg_label` output, `"Broker:acct"` for
  real account ids) while `expected_brokers_by_source` held bare broker
  names — under multi-account fan-out the sets could never match, so
  `mark_buy_signals_executed` never fired and the signal re-executed on
  every automate run (double-trade). Fixed: completion now reads bare
  `leg["broker"]` names from the RAW execution legs (`ok=True` only), and
  the mark condition is `expected ⊆ completed`. **Decided multi-leg
  semantics:** a broker counts as completed when **≥ 1 of its legs
  succeeded** — closest parity with the old internal-fan-out behavior,
  where the broker call's overall success (e.g. Fennel returns True if at
  least one account succeeded) marked the signal. A source is marked only
  when EVERY expected broker completed (unchanged). Sell triggers split
  into one order per broker share a source key; their per-order legs
  accumulate into the same completed set, so the equality-to-subset change
  is behavior-preserving for them.
- **Final-review M4 finding — only `batch.py` duplicated the DRY RUN
  banner.** Text mode printed it in the header block AND via
  `cli_response_fn`; deduped exactly like `cli/trade.py` (response-fn call
  gated to JSON mode). `automate.py` — cited in the review — emits the
  banner exactly once in text mode (verified empirically and pinned by
  `test_automate_dry_run_banner_prints_once_in_text_mode`); no change made
  there.
- **Final-review I2 — TUI session-init drift.** `submit_all_orders` (and
  `retry_timed_out_brokers`) never initialized broker sessions, so engine
  account discovery read `session_manager.sessions` cold on the first
  submission and warm afterwards. Nothing else in the TUI guarantees init
  (`tui/session_cache.py` only reads status; the holdings screen relies on
  the broker fns' lazy `get_session`). Both entry points now call
  `initialize_selected_sessions` before `validate_targets`, mirroring the
  CLI paths, with non-raising error display (TUI must not crash).

---

## File structure

| File | Change |
|---|---|
| `src/cli/common.py` | + `render_execution_result()` / `aggregate_execution_results()` |
| `src/agentic/router/_server.py` | + `validate_targets()` engine method (Task 2); − engine body (Task 7) |
| `src/cli/trade.py` | repoint to engine; dry-run rehearsal |
| `src/cli/batch.py`, `src/cli/automate.py` | repoint to engine |
| `src/tui/app.py` | repoint to engine (both entry points) |
| `src/agentic/cli_bridge.py` | **deleted** |
| `src/execution/engine.py` | receives the ExecutionEngine class body |
| `tests/test_execution_layering.py` | engine.py joins the no-agentic-imports lock |
| Golden + bridge tests | intentional flips / rewrites / deletions per task |

---

### Task 1: `render_execution_result` (the rendering layer)

**Files:** Modify `src/cli/common.py`; Create `tests/test_render_execution_result.py`

- [ ] Write failing tests first. Behaviors to pin:
  1. Single execution dict with 2 ok legs + 1 failed leg (across 2 brokers, one broker having taxable+ira accounts) renders to `{successful: <n_ok_brokers... >}` — **decide and pin the semantics: counts are per-LEG now, not per-broker** (a broker with 2 accounts contributes 2 to the counts). `statuses` entry: `{ticker, action (from side), successful: ["Robinhood:taxable", ...], failed: [...], skipped: [...]}` — legs render as `"Broker"` when account_id is `"primary"`/empty and `"Broker:account"` otherwise, so single-account output is byte-identical to today's.
  2. A `rejected=True` execution renders with `successful=0, failed=0, skipped=<leg or broker count>` and carries `reason` through into the status entry.
  3. `aggregate_execution_results(list_of_rendered)` sums counts and concatenates `statuses` (for batch/automate/TUI multi-order paths).
  4. `dry_run=True` executions render identically (the flag rides along; exit-code logic is unchanged).
- [ ] Implement in `src/cli/common.py` as pure functions (no I/O). Type the input loosely (`dict[str, Any]`) — the engine's dict is the contract.
- [ ] `compute_trade_exit_code` (src/cli_runtime.py) reads `{successful, failed, skipped}` — confirm rendered output satisfies it with a direct test.
- [ ] Full suite green (258 + new). Commit: `feat(cli): render_execution_result adapter for engine-native results`.

### Task 2: `ExecutionEngine.validate_targets`

**Files:** Modify `src/agentic/router/_server.py`; Modify `src/agentic/cli_bridge.py` (delegation shim only); Modify `tests/agentic/test_preflight_validation.py`

- [ ] Move the body of `cli_bridge.preflight_validate` (~219–287) onto the engine as `async def validate_targets(self, *, selected_brokers, action, quantity, ticker, price, validate_functions, timeout=..., progress_fn=None) -> tuple[list[str], list[tuple[str, str]]]` — same semantics (concurrent per-broker validate fns, timeout, `(validated, [(broker, reason)])`). Keep the signature compatible; `progress_fn` stays an optional callback.
- [ ] `cli_bridge.preflight_validate` becomes a one-line delegation to the engine (it dies in Task 6; the shim keeps every caller green between tasks).
- [ ] Repoint `tests/agentic/test_preflight_validation.py` at the engine method (these tests survive Task 6).
- [ ] Full suite green. Commit: `feat(engine): validate_targets (preflight moves onto the engine)`.

### Task 3: Repoint `cli/trade.py` (single-order path + dry-run rehearsal)

**Files:** Modify `src/cli/trade.py`; Modify `tests/test_cli_trade_golden.py`, `tests/agentic/test_cli_bridge_golden.py`

- [ ] Replace `apply_main_py_gate` + `execute_via_router` + `record_main_py_outcome` with:
  ```python
  engine = await get_engine()   # module-level lazy singleton mirroring cli_bridge.get_router(); build via ExecutionEngine.from_all_brokers(); import from execution, not agentic
  proposal = await engine.propose_order(ticker=..., qty=..., side=action, brokers=brokers_to_use, price=..., dry_run=context.dry_run)
  execution = await engine.execute_order(proposal_id=proposal["proposal_id"], dry_run=context.dry_run)
  results = render_execution_result(execution)
  ```
  GateError handling stays (propose raises it); rejection dicts from execute render via Task 1's helper. Where the lazy singleton lives: `cli/common.py` (shared by Tasks 4–5), including a `reset_engine()` test hook mirroring `reset_router()`.
- [ ] **Dry-run flip:** delete the `context.dry_run` short-circuit to `_build_dry_run_readiness` (~line 93). `--dry-run` now flows through the SAME propose/execute path with `dry_run=True`. Output labels the run as a rehearsal (message string includes "DRY RUN — full pipeline rehearsal, no orders placed"). `--mock-brokers` branch is untouched.
- [ ] **Golden test flips (intentional, per ADR step 0):**
  - `test_dry_run_does_not_touch_gate` → rewrite as `test_dry_run_is_full_pipeline_rehearsal`: with mocked engine, assert propose AND execute are called with `dry_run=True` and no broker trade fn is invoked.
  - `test_gate_intent_uses_one_primary_leg_per_broker` (bridge golden) → superseded in Task 6; for now mark/adjust minimally so the suite is green at this task's commit (it tests the bridge path which still exists until Task 6 — if it still passes untouched, leave it).
  - Multi-account golden: add a test with a mocked engine whose `propose_order` returns `accounts_by_broker={"Robinhood": ["taxable", "ira"], "Public": ["primary"]}` and whose execution has 3 legs — assert rendered counts reflect 3 legs (the ADR's announced behavior change).
- [ ] `_build_dry_run_readiness` in `cli/common.py`: if `batch.py`/`automate.py` still use it, leave the function; if `trade.py` was the only consumer, delete it in Task 6, not here.
- [ ] Smoke: `.venv/bin/python src/main.py buy 1 TSLA --dry-run --mock-brokers --output json` — exit 0. Also run the real `--dry-run` path with mocked engine in tests only (no live sessions in CI).
- [ ] Full suite green. Commit: `feat(cli): trade path on ExecutionEngine; --dry-run is a full rehearsal`.

### Task 4: Repoint `cli/batch.py` + `cli/automate.py`

**Files:** Modify both; adjust their tests (`tests/agentic/test_f5_v03_wiring.py` etc. only where they spy on bridge functions from these paths — full rewrites happen in Task 6)

- [ ] Same substitution, per order: loop `propose_order` (fail-fast on GateError to preserve `apply_main_py_gate_batch`'s first-rejection semantics), then `execute_order` per proposal, `render_execution_result` each, `aggregate_execution_results` for the final legacy dict. Preflight via `engine.validate_targets`.
- [ ] `automate` keeps `dry_run=False` on its live path (its own `--dry-run` handling: mirror whatever it does today — read the handler; if it had the readiness short-circuit, flip it exactly like Task 3).
- [ ] Full suite green (adjust only tests that spy on these two files' internals). Commit: `feat(cli): batch + automate on ExecutionEngine`.

### Task 5: Repoint `tui/app.py`

**Files:** Modify `src/tui/app.py` (both `submit_all_orders` and `retry_timed_out_brokers`)

- [ ] Same substitution; keep `progress_fn` behavior by emitting the same progress messages around propose/execute that `execute_via_router` emitted (read its ~290–405 body for the exact strings the TUI displays; preserve user-visible text).
- [ ] TUI widgets read `results["statuses"]` — rendered shape from Task 1 satisfies this; verify by running the TUI test(s) that exist and a manual smoke note (TUI has no automated harness — flag in report if true).
- [ ] Full suite green. Commit: `feat(tui): order submission on ExecutionEngine`.

### Task 6: Delete `cli_bridge.py`

**Files:** Delete `src/agentic/cli_bridge.py`; delete/rewrite its test files; sweep imports

- [ ] `grep -rn cli_bridge src tests` must return zero after this task.
- [ ] Test disposition (recon list): `test_main_py_gating.py`, `test_f5_v03_wiring.py`, `test_f5_v04_router_execution.py` — rewrite the behaviors that are still meaningful against the engine path (gate-before-execute ordering, batch fail-fast, audit entries written by enforcement core), delete what only tested bridge reshaping. `test_cli_bridge_golden.py` — replace with an engine-path golden pinning the real-accounts intent (the inverse of the old one-primary-leg pin). `test_preflight_validation.py` — already repointed (Task 2).
- [ ] `gate_error_to_exit_code`: relocate to `cli/common.py` if still referenced, else delete.
- [ ] Delete `_build_dry_run_readiness` if no consumers remain.
- [ ] Full suite green. Commit: `refactor(cli): delete cli_bridge — one propose path (ADR 0006 step 5)`.

### Task 7: Relocate the engine body to `execution/engine.py`

**Files:** Modify `src/agentic/router/_server.py`, `src/execution/engine.py`, `src/agentic/router/__init__.py`

- [ ] Move the `ExecutionEngine` class (and only what it needs: module constants like `DEFAULT_*_STORE_PATH`, `NullAccountStatusProvider`, `BrokerServerAccountStatusProvider`, `load_all_broker_specs`) into `src/execution/engine.py`. It must import ONLY from `execution/`, `enforcement/`, `brokers/`, stdlib — never `agentic/`. The `if TYPE_CHECKING: from signals.nasdaq import CalendarSignal` import moves with it.
- [ ] `src/agentic/router/_server.py` keeps: `build_router_fastmcp_server` (imports `ExecutionEngine` from `execution.engine`), the stdio entrypoint glue, and back-compat re-exports (`ExecutionEngine`, `Router = ExecutionEngine`) so existing imports (`from agentic.router._server import ...` in tests) stay valid.
- [ ] Pure move: no logic edits. `git log --follow` friendliness is nice but not required.
- [ ] Full suite green (this is the proof the move is pure). Commit: `refactor(execution): relocate ExecutionEngine body — import direction flipped (ADR 0006 step 2 complete)`.

### Task 8: Lock the layering + docs

**Files:** Modify `tests/test_execution_layering.py`, `docs/adr/0006-execution-engine-as-core.md`, `CLAUDE.md`, `CONTEXT.md`

- [ ] Add `"engine.py"` to the `AGENTIC_FREE` tuple; add a second assertion sweeping every module under `src/enforcement/` for `agentic` imports (ADR step 6 wording).
- [ ] ADR 0006: mark steps 3–6 done with dates; record the dry-run decision (full rehearsal, readiness check retired) and the per-leg counting semantics in the Consequences section; flip Status from Proposed → Accepted.
- [ ] CLAUDE.md: the Architecture section still documents `order_processor.py` / `OrderBatchProcessor` as the execution path (long stale) and now-deleted cli_bridge behavior — rewrite those subsections to describe the engine + adapters reality (keep it brief; point at the ADR). Update the `--dry-run` documentation to the rehearsal semantics.
- [ ] Full verification: `.venv/bin/python -m pytest tests/ -q` + `py_compile` sweep. Commit: `docs: ADR 0006 accepted; CLAUDE.md reflects engine-as-core`.

---

## Self-review notes

- Task ordering keeps the suite green at every commit: shims (Tasks 1–2) before repoints (3–5), deletion (6) only after no caller remains, body move (7) after `_server.py` stops changing, lock (8) last.
- The known behavior changes are all announced and tested: per-leg counts (Task 1/3), dry-run rehearsal (Task 3), multi-account fan-out (Task 3 golden).
- Escalation rule for implementers: if a recon fact contradicts the code (line drift, signature mismatch, an unexpected caller of cli_bridge), report NEEDS_CONTEXT rather than improvising.
