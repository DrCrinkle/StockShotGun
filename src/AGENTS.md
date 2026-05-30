# CORE APPLICATION LAYER KNOWLEDGE BASE

**Location:** `src/`
**Focus:** CLI/TUI dispatch, execution runtime, automation orchestration

## OVERVIEW
Application control plane. Routes actions, enforces runtime contracts, and coordinates broker + TUI layers.
Root AGENTS.md conventions apply; this file only covers src-layer routing/runtime specifics.

## STRUCTURE
```
src/
├── main.py            # Dispatcher: argparse + run_cli routing; delegates handlers to cli/
├── cli/               # Command handlers: trade, batch, automate, sweep + shared common
├── order_processor.py # Retired fan-out; now only the current_broker context var
├── cli_runtime.py     # ExitCode, CliRuntimeError, ExecutionContext, JSON response envelopes
├── automation_recap.py# SQLite recap ingestion + due-buy/due-sell extraction helpers
├── setup.py           # Interactive credential wizard writing .env entries
├── agentic/           # Router + per-broker MCP servers + cli_bridge (gate/execute path)
├── enforcement/       # Order enforcement gate: limits, freeze, circuit breaker, audit
├── brokers/           # Broker adapter layer (see src/brokers/AGENTS.md)
└── tui/               # urwid terminal interface (see src/tui/AGENTS.md)
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Add/change CLI action | `main.py` (`run_cli` dispatch) + `cli/` handler | Keep action routing + output envelope behavior aligned |
| Buy/sell flow | `cli/trade.py` (`run_trade`) | Pre-flight → gate → execute via Router |
| Batch order file flow | `cli/batch.py` (`_validate_batch_orders`, `_run_batch_from_file`) | Validates schema and broker selection before execution |
| Automation from recap | `cli/automate.py` (`_run_automate_from_recap`) + `automation_recap.py` | Recap parsing + due-signal execution path |
| Runtime envelope/errors | `cli_runtime.py` | Source of truth for machine-readable CLI responses |
| Order execution + gating | `agentic/cli_bridge.py` | `preflight_validate` → `apply_main_py_gate*` → `execute_via_router` |
| Setup credentials | `setup.py` | Writes `.env`; not packaging/build setup |

## CONVENTIONS (SRC-SPECIFIC)
- Keep `main.py` as dispatcher; put command handlers in `cli/` and broker-specific behavior in broker modules.
- Route all CLI JSON output through `build_response_envelope` helpers.
- Respect `ExecutionContext`: request IDs, non-interactive mode, output mode, and log file path.
- When non-interactive mode is set, raise `CliRuntimeError` instead of blocking for user input.
- Execute orders through the gate/Router path (`cli_bridge.preflight_validate` → `apply_main_py_gate*` → `execute_via_router`); the gate call MUST precede execution. Do not call broker SDKs directly from handlers.
- Keep broker function lookup centralized through `brokers/registry.py` (the single source of truth; ADR 0004). Resolve via `registry.resolve_trade/holdings/validate` or `registry.broker_functions_map()`.

## ANTI-PATTERNS (SRC LAYER)
- Adding new action logic directly in argument parsing blocks without wiring into `run_cli`/runtime envelope flow.
- Returning ad-hoc JSON/print payloads instead of standardized envelope + exit code handling.
- Bypassing `_validate_batch_orders` before running batch trades.
- Duplicating broker readiness checks outside shared helper paths.
- Turning `setup.py` into setuptools logic (it is a credential wizard module).

## COMMANDS
```bash
uv run --python 3.14 python src/main.py --help
./stockshotgun health --output json
./stockshotgun automate --dry-run --output json
bash scripts/verify.sh
```

## NOTES
- `main.py` is intentionally large and central; prefer surgical edits.
- Root `stockshotgun` shim and `pyproject.toml` console script both target `main:main`.
- `README.md` still references `requirements.txt`; project-standard path is `uv sync`.
