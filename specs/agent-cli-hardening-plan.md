# Agent CLI Hardening Plan

## Objective

Make CLI interaction deterministic, machine-readable, and safe for autonomous agents without breaking current human-first defaults.

## Deliverables

- `--output json` for all CLI actions
- `--non-interactive` fail-fast behavior (no blocking prompts)
- `--dry-run` trade preflight mode (no orders placed)
- `--from-file <orders.json>` batch execution
- `health` command for broker readiness
- Stable exit code contract
- Structured JSONL logging with correlation IDs
- Mock broker mode for safe automation/CI

## Milestones

### M1 - Runtime Contract Foundation

Status: `implemented`

Implement:
- Unified response envelope for text/json execution paths
- Centralized exit code constants and mapping
- `ExecutionContext` object to carry request flags across CLI flow

Success criteria:
- Existing no-flag behavior unchanged
- Error categories map to deterministic exit codes

Implementation notes:
- Added `src/cli_runtime.py` with `ExitCode`, `ExecutionContext`, response envelope builder, and trade exit-code mapping
- Wired CLI runtime errors and deterministic `SystemExit` codes through `src/main.py`
- Added reserved CLI flags (`--output`, `--non-interactive`, `--dry-run`, `--log-format`, `--request-id`) for follow-on milestones

### M2 - JSON Output Mode

Status: `implemented`

Implement:
- Global `--output text|json` flag in CLI
- One-output adapter for all commands (`buy`, `sell`, `holdings`, `setup`, `health`)
- JSON error payloads with same envelope shape

Success criteria:
- JSON mode emits valid, parseable JSON only (no mixed text)

Implementation notes:
- `src/main.py` now returns structured `(exit_code, data)` from CLI handlers and emits envelope output in JSON mode
- Added JSON envelopes for both runtime errors and argparse parsing failures (via `RuntimeArgumentParser`)
- JSON mode suppresses human text output and returns structured payloads for `setup`, `holdings`, and trade actions

### M3 - Non-Interactive Mode

Status: `implemented`

Implement:
- Global `--non-interactive` flag
- Guard prompt paths in CLI and broker flows
- Return typed failure + exit code when interactive input is required

Success criteria:
- No CLI hangs waiting for stdin with `--non-interactive`

Implementation notes:
- Added non-interactive input guard in `src/tui/input_handler.py`; intercepted `input()` now raises a typed runtime error when non-interactive mode is enabled
- Wired non-interactive interception lifecycle in `src/main.py` using `setup_tui_input_interception()` + restore in `finally`
- Added explicit fail-fast for `setup` action in non-interactive mode with exit code `7`
- Updated session init path in `src/brokers/session_manager.py` to re-raise non-interactive prompt errors from gathered tasks
- Updated shared broker login helper in `src/brokers/base.py` to avoid swallowing non-interactive prompt errors

### M4 - Dry-Run Mode

Status: `implemented`

Implement:
- Global `--dry-run` for trade actions
- Preflight checks (args, broker selection, credentials, optional session readiness)
- Planned operations summary per broker

Success criteria:
- No trade API calls in dry-run path

Implementation notes:
- Added a `--dry-run` preflight branch in `src/main.py` that returns before `order_processor.process_orders(...)`
- Dry-run now reports per-broker readiness (trade function presence + initialized session) and list of ready brokers
- Text mode prints a concise preflight summary; JSON mode returns structured readiness payload
- Dry-run exit code is deterministic: success when at least one broker is ready, credential/config missing when none are ready

### M5 - Batch Orders from File

Status: `implemented`

Implement:
- `--from-file <path>` for batch orders
- Strict order schema validation with per-item errors
- Execute via `OrderBatchProcessor` for consistency

Success criteria:
- Deterministic per-order and per-broker result reporting

Implementation notes:
- Added `--from-file <path>` in `src/main.py` with support for either `[ ... ]` or `{ "orders": [ ... ] }` JSON payloads
- Added strict per-order validation with aggregated actionable errors (`order[index]: ...`) surfaced in the JSON error envelope
- Added batch execution path using existing `order_processor.process_orders(...)` for live runs
- Added batch dry-run preflight output with per-order readiness details and deterministic exit behavior
- Added support for global `--broker` override on batch files

### M6 - Health Command

Status: `implemented`

Implement:
- New action: `health`
- Per-broker status (credentials, session/auth readiness, enabled state)
- Broker filtering support with `--broker`

Success criteria:
- Machine-readable readiness matrix; aggregate status reflected in exit code

Implementation notes:
- Added `health` action in `src/main.py` parser and runtime routing
- Health returns per-broker readiness data: credential presence/missing vars, session initialization state, and trade/holdings capability flags
- Added human-readable text output and JSON envelope data output for `health`
- Health exit code is deterministic: success when at least one broker is ready, config/credential missing when none are ready

### M7 - Structured Logs

Status: `implemented`

Implement:
- `--log-format text|jsonl`, `--log-file`, `--request-id`
- Consistent event schema with request/order/broker correlation fields

Success criteria:
- End-to-end run traceable by a single `request_id`

Implementation notes:
- Added JSONL event emitter in `src/main.py` (`command_start`, `command_success`, `command_error`) with `request_id` correlation
- Added `--log-file` CLI option to route JSONL logs to file; default JSONL sink is stderr to avoid polluting stdout responses
- Extended `ExecutionContext` in `src/cli_runtime.py` to carry `log_file`

### M8 - Mock Broker Mode

Status: `implemented`

Implement:
- `--mock-brokers` (or equivalent env toggle)
- Deterministic simulated broker responses for trade/holdings/health
- Zero live credentials/network dependency in mock mode

Success criteria:
- Full automation rehearsable locally and in CI without side effects

Implementation notes:
- Added `--mock-brokers` flag in `src/main.py` and `ExecutionContext`
- Implemented deterministic mock responses for trade, batch, holdings, and health actions
- Mock mode bypasses live broker session initialization and order placement paths
- Mock outputs include `mock: true` to make mode explicit for automation consumers

## File-by-File Change Map

- `src/main.py`
  - Add new CLI flags/actions
  - Build `ExecutionContext`
  - Route command handling through output/error adapters
- `src/order_processor.py`
  - Add dry-run-aware execution path
  - Ensure per-broker structured result details
- `src/tui/broker_functions.py`
  - Keep dispatch compatibility while adding health and mock-aware wiring
- `src/brokers/base.py`
  - Shared interfaces/types for status, errors, and logging metadata
  - Optional mock abstraction hooks
- `src/brokers/session_manager.py`
  - Readiness/status helpers reused by `health` and dry-run preflight
- `src/setup.py`
  - Optional non-interactive setup checks/reporting (no prompt mode)
- `stockshotgun`
  - Ensure launcher passes new CLI semantics consistently
- `scripts/verify.sh`
  - Add JSON-mode smoke checks once features land
- `AGENT_RUNBOOK.md`
  - Add new command examples and expected machine-readable outputs
- `AGENT_TASKS.md`
  - Add recipes for health checks, batch orders, and dry-run workflows
- `specs/agent-cli-hardening-plan.md`
  - Track milestone status and rollout notes

## Exit Code Contract

- `0` success
- `2` invalid args/usage
- `3` config/credential missing
- `4` auth/session failure
- `5` partial broker execution failure
- `6` full broker execution failure
- `7` interactive input required in non-interactive mode
- `10` unexpected internal error

## JSON Envelope (Draft)

```json
{
  "ok": true,
  "command": "holdings",
  "request_id": "req_123",
  "timestamp": "2026-02-05T21:00:00Z",
  "data": {},
  "warnings": [],
  "errors": []
}
```

## Verification Matrix

### Core command behavior

- [ ] `uv run --python 3.14 python src/main.py --help`
- [ ] `uv run --python 3.14 python src/main.py health --output json`
- [ ] `uv run --python 3.14 python src/main.py holdings TSLA --broker Robinhood --output json`

### Non-interactive safety

- [ ] Prompt-required path with `--non-interactive` exits non-zero and does not block
- [ ] Error payload includes actionable reason in JSON mode

### Dry-run correctness

- [ ] `buy/sell --dry-run` returns planned actions only
- [ ] No broker trade call executed in dry-run path

### Batch correctness

- [ ] Valid `orders.json` executes and reports per-item results
- [ ] Invalid `orders.json` reports per-item validation failures and exits non-zero

### Logging

- [ ] JSONL log lines are valid JSON objects
- [ ] `request_id` present on every emitted event

### Mock mode

- [ ] Mock mode works without live credentials
- [ ] Health + dry-run + batch all function with deterministic mock responses

### Regression gate

- [ ] `./scripts/verify.sh` passes

## Rollout Strategy

1. Ship M1 + M2 first behind default-compatible behavior
2. Add M3 + M4 for safer automation execution
3. Add M5 + M6 for operational coverage
4. Add M7 + M8 for observability and CI-safe full-flow testing

## Notes

- Preserve human-readable default output unless `--output json` is explicitly set
- Never expose credential values in JSON payloads or logs
- For bugfixes during rollout, prefer minimal scope; no opportunistic refactors
