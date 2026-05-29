# STOCKSHOTGUN - PROJECT KNOWLEDGE BASE

**Generated:** 2026-02-16
**Commit:** 0489ae9
**Branch:** main
**Scale:** ~10k lines Python, 13 brokers
**Runtime:** Python 3.14 (free-threaded compatible)

## OVERVIEW
Async-first multi-broker trading app for reverse split arbitrage. Submits orders concurrently across 13 brokers via CLI (with JSON envelope output) or TUI (urwid). Uses `uv` for dependency management.

## STRUCTURE
```
./
├── stockshotgun           # Entry shim (adds src/ to sys.path, calls src.main.main)
├── src/
│   ├── main.py            # God module: argparse, CLI dispatch, batch/automation (1226 lines)
│   ├── order_processor.py # OrderBatchProcessor: concurrent execution w/ per-broker timeouts
│   ├── cli_runtime.py     # ExitCode enum, ExecutionContext, JSON response envelopes
│   ├── automation_recap.py# SQLite-backed recap store for automated runs
│   ├── setup.py           # Credential wizard (NOT setuptools) → writes .env
│   ├── AGENTS.md          # Core application layer map (CLI runtime + automation)
│   ├── brokers/           # 13 broker integrations (see src/brokers/AGENTS.md)
│   └── tui/               # Terminal UI (see src/tui/AGENTS.md)
├── tokens/                # OAuth persistence (Schwab) + Wells Fargo browser profile
├── scripts/verify.sh      # Manual validation: mypy + py_compile + smoke tests
├── specs/                 # Architecture plans (agent-cli hardening, ticker rules)
└── logs/                  # SQLite automation DB + JSON trace logs
```

## WHERE TO LOOK
| Task | Location | Notes |
|------|----------|-------|
| Add broker | `src/brokers/{broker}.py` + register in 5 places | See src/brokers/AGENTS.md |
| Order execution | `src/order_processor.py` | `OrderBatchProcessor` with `asyncio.gather` |
| CLI dispatch | `src/main.py` → `run_cli()` | Actions: buy, sell, holdings, health, automate, setup |
| JSON output | `src/cli_runtime.py` | `build_response_envelope()`, `ExitCode` enum |
| TUI flow | `src/tui/app.py` → `run_tui()` | See src/tui/AGENTS.md |
| Rate limits | `src/brokers/base.py` | `RateLimiter.BROKER_LIMITS` dict |
| Session lifecycle | `src/brokers/session_manager.py` | Lazy-loaded, concurrent-safe with `asyncio.Lock` |
| Validation | `src/brokers/{broker}.py` → `{broker}Validate()` | Pre-trade ticker/order validation |
| Broker registry | `src/tui/broker_functions.py` | `BROKER_CONFIG` maps names → trade/holdings/validate fns |
| Verification | `scripts/verify.sh` | mypy, py_compile, smoke tests |

## CODE MAP
| Symbol | Type | Location | Refs | Role |
|--------|------|----------|------|------|
| `run_cli` | Function | `src/main.py:739` | 2 | Primary CLI action dispatcher |
| `OrderBatchProcessor` | Class | `src/order_processor.py:16` | 2 | Concurrent broker execution + validation |
| `BrokerSessionManager` | Class | `src/brokers/session_manager.py:33` | 4 | Lazy session lifecycle + per-broker locks |
| `run_tui` | Function | `src/tui/app.py:26` | 5 | urwid entry point and UI orchestration |
| `build_response_envelope` | Function | `src/cli_runtime.py:47` | N/A | Standard JSON response contract |

## CONVENTIONS

**Async-First (MANDATORY):**
- All broker operations must be `async`
- Wrap blocking SDK calls with `asyncio.to_thread()`
- Never call synchronous SDK methods directly in async context

**Rate Limiting (MANDATORY):**
- Every trade/holdings/validate function: `await rate_limiter.wait_if_needed("BrokerName")` as first line
- Limits configured in `RateLimiter.BROKER_LIMITS` in `brokers/base.py`

**Shared Infrastructure:**
- Use `http_client` from `brokers/base.py` (connection pooling, HTTP/2) — never create new clients
- Use `api_cache` for static data (5-min TTL)
- Cache account IDs/profiles during session initialization

**Thread Safety (Python 3.14 no-GIL):**
- All shared state in `base.py` and `session_manager.py` protected by `threading.Lock`
- Asyncio primitives (`asyncio.Lock`) remain for async coordination

**CLI Output:**
- `--output json` → structured JSON envelope via `build_response_envelope()`
- `--non-interactive` → disables TUI, raises `CliRuntimeError` instead of `input()` prompts
- `--request-id` → tracks requests through the pipeline

**Broker Integration Pattern:**
- Each broker module implements: `{broker}Trade`, `{broker}GetHoldings`, `get_{broker}_session`
- Most also implement `{broker}Validate` for pre-trade validation
- Registration: `__init__.py` exports, `session_manager.py` BROKER_MODULES, `broker_functions.py` BROKER_CONFIG, `base.py` BrokerConfig, `setup.py` creds

## ANTI-PATTERNS (THIS PROJECT)

**Forbidden:**
- Blocking SDK calls without `asyncio.to_thread()` wrapper
- Missing `rate_limiter.wait_if_needed()` before API calls
- Creating new HTTP clients (use shared `http_client`)
- Committing `.env` file (credentials in plaintext)
- `time.sleep()` or `threading.Event.wait()` anywhere (freezes TUI event loop)

**Deprecated:**
- Webull username/password login (broken Sept 2025, use pre-obtained tokens)
- `abc.abstractclassmethod` (use `@classmethod` + `@abstractmethod`)
- `requirements.txt` (use `uv sync` / `pyproject.toml`)

## UNIQUE STYLES

**Dual Interface:**
- `main.py`: No args → TUI mode, args → CLI mode
- TUI intercepts `builtins.input()` for MFA prompts via modal dialogs

**Entry Point Shim:**
- Root `stockshotgun` script manually adds `src/` to `sys.path`
- `pyproject.toml` also defines `stockshotgun = "main:main"` console script
- `setup.py` is credential wizard, not build script

**Browser Automation (Wells Fargo, Chase, SoFi):**
- Class-based: `WellsFargoClient`/`ChaseClient` with `__aenter__`/`__aexit__`
- Lazy auth + shared helpers from `browser_utils` (`create_browser`, `navigate_and_wait`)
- `HEADLESS=true/false` controls visibility

**Flat `src/` Layout:**
- No nested packages beyond `brokers/` and `tui/`
- `tokens/` holds OAuth data + full Chromium browser profile

## COMMANDS

```bash
# Setup
uv sync                                        # Install deps (requires Python 3.14)
./stockshotgun setup                           # Configure broker credentials (.env)

# Run
./stockshotgun                                 # TUI mode (interactive)
./stockshotgun buy 10 TSLA                     # CLI: market order
./stockshotgun sell 5 AAPL 175.50              # CLI: limit order
./stockshotgun holdings TSLA --broker Robinhood
./stockshotgun buy 10 TSLA --output json       # JSON envelope output
./stockshotgun buy 10 TSLA --non-interactive   # Agent/machine mode

# Validate
bash scripts/verify.sh                         # mypy + py_compile + smoke tests
```

## NOTES

**Security:**
- `.env` contains credentials → NEVER commit (in .gitignore)
- Credentials + session tokens are plaintext at rest (`.env`, `tokens/`, `did.bin`)

**Broker-Specific:**
- **Webull**: Pre-obtained tokens required (Chrome extension). Login API broken since Sept 2025

**Gotchas:**
- No `tests/` directory (despite `.pytest_cache`)
- No CI/CD — manual validation only via `scripts/verify.sh`
- Treat `tokens/wellsfargo_profile/` as runtime browser state; do not traverse for code exploration
