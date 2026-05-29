# StockShotGun Code Review Improvements Plan

## Goals
- Reduce trading-flow risk by improving error visibility and deterministic outcomes.
- Improve maintainability by reducing coupling and duplicated configuration.
- Keep behavior stable while refactoring: no trading semantics changes during early phases.

## Non-Goals (for now)
- No rewrite of broker implementations.
- No change to order placement semantics (timing, retry policy, success criteria) until typed results are in place.
- No TUI input bridge rewrite until end-to-end validation is repeatable.

## Progress Snapshot (2026-02-05)
- Phase 1.1: In progress (substantial)
  - Replaced bare `except:` blocks in `src/brokers/wellsfargo.py`.
  - Reduced silent exception swallowing in `src/brokers/chase.py` and `src/brokers/wellsfargo.py` with debug logging in transient polling/select paths.
  - Narrowed several broad fallback exceptions in selector/evaluate loops to explicit transient classes.
- Phase 1.2: In progress (substantial)
  - Added logging in `src/order_processor.py` for timeout/error/critical batch failures.
  - Added logging in `src/brokers/session_manager.py` for unknown broker/getter and shutdown errors.
  - Added shared `broker_event(...)` helper in `src/brokers/base.py` and migrated high-value `print()` paths in Chase/Wells error/auth/session flows.
  - Remaining: continue incremental migration of broker `print()` calls and move toward structured event payloads.

## Phase 1 - Quick Wins (Low Risk, High Return)

### 1.1 Exception Hygiene in Browser Brokers
Scope:
- `src/brokers/wellsfargo.py`
- `src/brokers/chase.py`

Actions:
- Replace bare `except:` with explicit exception classes where possible.
- Avoid swallowing errors silently; preserve traceback context in logs.
- Ensure cleanup paths are consistent when auth/browser operations fail.

Success Criteria:
- No bare `except:` in broker modules.
- Failures surface with actionable broker/account context.

Status:
- In progress. Bare `except:` removed from Wells Fargo and many transient paths now log context; broad exception handling still exists in non-transient operation blocks.

### 1.2 Structured Logging Foundation
Scope:
- `src/order_processor.py`
- `src/brokers/*.py`
- `src/tui/response_handler.py`

Actions:
- Introduce a minimal structured logging interface for core/orchestration layers.
- Keep user-facing rendering in CLI/TUI layer; core emits structured events.
- Reduce direct `print()` use in non-UI modules incrementally.

Success Criteria:
- Core paths can emit machine-readable events.
- TUI/CLI still show equivalent user feedback.

Status:
- In progress. Logging scaffolding exists (`order_processor`, `session_manager`, `broker_event` helper), and high-value broker paths now dual-emit log+stdout.
- Remaining: define/introduce machine-readable event schema and route UI rendering from that schema.

---

## Phase 2 - Contract Hardening (Medium Risk)

### 2.1 Typed Broker Result Contract
Scope:
- `src/order_processor.py`
- `src/tui/broker_functions.py`
- `src/brokers/*.py`

Actions:
- Replace `True/False/None` broker responses with a typed result object:
  - `status` (`success` | `failed` | `skipped` | `timeout`)
  - `broker`
  - `message`
  - optional `details`
- Update `OrderBatchProcessor` to aggregate by typed status only.

Success Criteria:
- No broker trade path returns ambiguous boolean tri-state.
- Aggregation and summary are driven by typed statuses.

### 2.2 Typed Broker Exceptions
Scope:
- `src/brokers/base.py`
- `src/brokers/*.py`

Actions:
- Introduce a small exception hierarchy (auth, rate-limit, order-submit, data-parse).
- Map low-level exceptions to typed broker exceptions near source.

Success Criteria:
- Error handling paths can branch by exception type rather than string parsing.

---

## Phase 3 - Correctness Improvements

### 3.1 Per-Request Rate Limiting in Fan-Out Paths
Scope:
- `src/brokers/schwab.py`
- `src/brokers/robinhood.py`
- `src/brokers/webull.py`
- Other multi-account brokers as needed.

Actions:
- Apply `await rate_limiter.wait_if_needed("BrokerName")` before each outbound API call in account loops.
- Preserve concurrency where appropriate, but gate external calls safely.

Success Criteria:
- Fan-out flows respect broker rate limits per request.
- Reduced likelihood of burst-induced throttling.

### 3.2 Timeout Alignment
Scope:
- `src/order_processor.py`
- Browser brokers with long MFA/auth flows.

Actions:
- Align broker timeouts with realistic auth + order durations.
- Keep timeout values centralized and documented.

Success Criteria:
- Fewer false timeouts during legitimate long auth flows.

---

## Phase 4 - Maintainability Refactors (After Contracts Stabilize)

### 4.1 Consolidate Broker Registry
Scope:
- `src/brokers/base.py`
- `src/brokers/session_manager.py`
- `src/tui/broker_functions.py`

Actions:
- Create one source of truth for broker metadata (display name, session key, module, capabilities, env vars).
- Remove duplicate mappings.

Success Criteria:
- Adding/removing a broker requires changes in one registry location.

### 4.2 Decompose Large Files by Responsibility
Targets:
- `src/main.py`
- `src/brokers/chase.py`
- `src/brokers/wellsfargo.py`
- `src/tui/app.py`

Actions:
- Split by concern (auth/session/trade/holdings/formatting), preserving public function contracts.
- Avoid behavior changes during moves.

Success Criteria:
- Reduced file size and clearer module boundaries.
- No functional regressions in trade flows.

---

## Phase 5 - TUI Input Bridge Hardening (Last)
Scope:
- `src/tui/input_handler.py`

Actions:
- Replace private event-loop stepping/monkeypatch-heavy behavior with a more explicit async prompt bridge.
- Keep existing user interaction behavior unchanged.

Success Criteria:
- MFA prompts remain responsive.
- Reduced dependence on private event loop internals.

---

## Suggested PR Slices
1. Exception hygiene in Wells Fargo/Chase. (In progress)
2. Structured logging/events scaffolding in core paths. (In progress)
3. Continue broker `print()` migration to `broker_event` for high-signal paths.
4. Typed broker result model + order processor aggregation update.
5. Typed broker exception hierarchy adoption.
6. Rate limiting fixes in fan-out brokers.
7. Timeout alignment and centralization.
8. Unified broker registry.
9. Large-file decomposition and TUI input bridge hardening.

## Verification Per PR
- `mypy . --show-error-codes --pretty --ignore-missing-imports`
- `python3 -m py_compile main.py src/brokers/*.py src/tui/*.py setup.py`
- Targeted dry-run/manual checks for broker auth + order submission paths impacted by that PR.
