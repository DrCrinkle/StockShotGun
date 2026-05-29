# CORE APPLICATION LAYER KNOWLEDGE BASE

**Location:** `src/`
**Focus:** CLI/TUI dispatch, execution runtime, automation orchestration

## OVERVIEW
Application control plane. Routes actions, enforces runtime contracts, and coordinates broker + TUI layers.
Root AGENTS.md conventions apply; this file only covers src-layer routing/runtime specifics.

## STRUCTURE
```
src/
├── main.py            # Primary dispatcher: argparse, CLI/TUI routing, automate + batch file flows
├── order_processor.py # Concurrent execution engine with validation, timeout, and retry behavior
├── cli_runtime.py     # ExitCode, CliRuntimeError, ExecutionContext, JSON response envelopes
├── automation_recap.py# SQLite recap ingestion + due-buy/due-sell extraction helpers
├── setup.py           # Interactive credential wizard writing .env entries
├── brokers/           # Broker adapter layer (see src/brokers/AGENTS.md)
└── tui/               # urwid terminal interface (see src/tui/AGENTS.md)
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Add/change CLI action | `main.py` (`main`, `run_cli`) | Keep action routing + output envelope behavior aligned |
| Batch order file flow | `main.py` (`_validate_batch_orders`, `_run_batch_from_file`) | Validates schema and broker selection before execution |
| Automation from recap | `main.py` (`_run_automate_from_recap`) + `automation_recap.py` | Recap parsing + due-signal execution path |
| Runtime envelope/errors | `cli_runtime.py` | Source of truth for machine-readable CLI responses |
| Concurrent order execution | `order_processor.py` | Broker-level timeout/validation/execution orchestration |
| Setup credentials | `setup.py` | Writes `.env`; not packaging/build setup |

## CONVENTIONS (SRC-SPECIFIC)
- Keep `main.py` as dispatcher; push broker-specific behavior into broker modules.
- Route all CLI JSON output through `build_response_envelope` helpers.
- Respect `ExecutionContext`: request IDs, non-interactive mode, output mode, and log file path.
- When non-interactive mode is set, raise `CliRuntimeError` instead of blocking for user input.
- Use `OrderBatchProcessor` for multi-broker order execution; do not duplicate gather/timeout logic in actions.
- Keep broker function lookup centralized through `tui/broker_functions.py` mappings.

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
