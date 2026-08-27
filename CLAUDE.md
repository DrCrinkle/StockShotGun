# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

StockShotGun is a multi-broker trading application that allows submitting orders to multiple brokerage accounts simultaneously. It's designed for reverse split arbitrage trading where speed and coordination across multiple brokers is essential.

The application supports both CLI and TUI (Terminal User Interface) modes for flexibility in different use cases.

## Architecture

### Core Components

**src/brokers/** - Modular broker integrations
- Each broker has its own module (13 brokers supported: Robinhood, Tradier, TastyTrade, Public, Firstrade, Fennel, Schwab, BBAE, DSPAC, SoFi, Webull, Wells Fargo, Chase)
- All broker modules follow a consistent pattern with two main functions:
  - `{broker}Trade(side, qty, ticker, price)` - Execute trades
  - `{broker}GetHoldings(ticker=None)` - Retrieve holdings
- Most brokers use API-based authentication; Wells Fargo and Chase use browser automation (see Browser Automation Pattern section)
- `registry.py` - Single source of truth for broker identity and lazy function bindings (ADR 0004)
- `_redbridge_broker.py` - Shared factory behind BBAE and DSPAC (both Redbridge Securities); each broker module is a ~13-line shim over `make_redbridge_broker`
- `browser_utils.py` - Shared zendriver plumbing (`create_browser`, `stop_browser`, page-readiness polling) used by Wells Fargo and Chase
- `base.py` - Shared infrastructure including:
  - `BrokerConfig` - Centralized broker configuration (credentials, session keys, enabled status)
  - `http_client` - Shared async HTTP client with connection pooling and HTTP/2 support
  - `rate_limiter` - Global rate limiter to prevent API throttling
  - `api_cache` - In-memory cache for API responses
  - Retry logic with exponential backoff
- `session_manager.py` - Manages authentication sessions across all brokers
  - Sessions are initialized once and reused to avoid repeated logins
  - Supports selective broker initialization to reduce startup time
  - Each broker module provides a `get_{broker}_session()` function

**src/tui/** - Terminal User Interface components
- `app.py` - Main TUI application entry point with `run_tui()` function
- `config.py` - TUI configuration and display constants
- `holdings_view.py` - Display broker holdings in the TUI
- `response_handler.py` - Manages broker response display
- `input_handler.py` - Intercepts Python's `input()` for TUI compatibility
- `session_cache.py` - Caches session status to avoid redundant initialization
- (`broker_functions.py` is gone — broker name→function maps come from `brokers.registry.broker_functions_map()`)

**src/main.py** - Application entry point (launched from the repo root via the `./stockshotgun` shim, which puts `src/` on `sys.path`)
- If arguments provided → CLI mode
- If no arguments → TUI mode
- Handles broker selection via `--broker` flag or defaults to all configured brokers
- Dispatches to `src/cli/` handlers, which execute orders through `ExecutionEngine` (see below)

**src/execution/engine.py** - `ExecutionEngine`, the execution core (ADR 0006)
- Every surface — CLI (`trade`/`batch`/`automate`), TUI, and the agentic/MCP router — is a thin adapter over this one engine
- Core API: `propose_order(...)` → `execute_order(proposal_id, ...)` (two-phase propose/execute), plus `validate_targets(...)` for pre-flight broker validation
- Adapters reach it via `src/cli/common.py`'s `get_engine()` (lazy singleton) and render its native per-leg result via `render_execution_result()` / `aggregate_execution_results()`
- `src/order_processor.py` is retired — the `OrderBatchProcessor` direct-broker fan-out is gone; only a `current_broker` context var survives (consumed by `src/tui/response_handler.py` to label output)
- Full history and rationale: `docs/adr/0006-execution-engine-as-core.md` (Accepted)

**src/setup.py** - Interactive credential setup wizard
- Validates existing credentials before prompting for new ones
- Stores credentials in `.env` file (never commit this file)

### Key Design Patterns

1. **Async-First Architecture**: All broker operations use `asyncio` for concurrent execution. `ExecutionEngine.execute_order` fans out per-account (not just per-broker — a broker with taxable + IRA accounts submits both legs concurrently), so trades are submitted simultaneously rather than sequentially.

2. **Engine-as-Core, Adapters at the Edge (ADR 0006)**: CLI, TUI, and the agentic/MCP router are all thin clients of the same `ExecutionEngine` (`src/execution/engine.py`) — one propose path, one execute path, one result type. This provides:
   - Real per-account fan-out for every caller, not just agents
   - `--dry-run` as a full-pipeline rehearsal: `propose_order(dry_run=True)` + `execute_order(dry_run=True)` exercises limits, freeze list, reconciliation, and token minting with no orders placed — not a credentials-only readiness check
   - Consistent error handling and status reporting via `render_execution_result` / `aggregate_execution_results` (`src/cli/common.py`)
   - Enforcement gates (limits, freeze, circuit breaker, audit) run once, in `src/enforcement/`, for every surface
   - See `docs/adr/0006-execution-engine-as-core.md` for the full before/after and migration history

3. **Centralized Configuration**: `src/brokers/registry.py` is the single source of truth for broker identity and function bindings (ADR 0004). `BrokerConfig`, the session manager, the CLI/TUI function maps, and the agentic router all derive from it. When adding a new broker:
   - Create the broker module with trade/holdings/validate/get_session functions
   - Add ONE `BrokerSpec(...)` entry to `_SPECS` in `src/brokers/registry.py` (lazy `"module:symbol"` refs — importing the registry imports no broker SDK)
   - Add a rate limit to `RateLimiter.BROKER_LIMITS` in `src/brokers/base.py`

4. **Session Management**: The `BrokerSessionManager` handles authentication and session lifecycle. Sessions are lazy-loaded and cached to minimize login overhead.

5. **Error Handling**: Individual broker (or per-account leg) failures don't halt the entire operation. `ExecutionEngine.execute_order` reports per-leg `ok`/`reason` results independently; `render_execution_result` renders those into `successful`/`failed`/`skipped` counts per leg, so some legs can complete while others fail without aborting the batch.

### Concurrency & Async Patterns

The application implements several patterns to ensure true concurrent execution and responsive UI:

1. **Blocking SDK Calls**: Many broker SDKs are synchronous. All blocking calls are wrapped with `asyncio.to_thread()` to prevent blocking the event loop:
   ```python
   # Bad: Blocks the event loop
   async def myBrokerTrade(side, qty, ticker, price):
       result = broker_sdk.place_order(ticker, qty)  # BLOCKING

   # Good: Runs in thread pool
   async def myBrokerTrade(side, qty, ticker, price):
       result = await asyncio.to_thread(broker_sdk.place_order, ticker, qty)
   ```

2. **Rate Limiting**: All broker modules use the shared rate limiter to prevent API throttling. Add rate limiting before API calls:
   ```python
   from .base import rate_limiter

   async def myBrokerTrade(side, qty, ticker, price):
       await rate_limiter.wait_if_needed("MyBroker")  # ALWAYS ADD THIS
       # ... API calls
   ```

   Per-broker rate limits are configured in `src/brokers/base.py` in the `RateLimiter.BROKER_LIMITS` dict.

3. **Shared HTTP Client**: Use the shared async client from `src/brokers/base.py` for connection pooling and HTTP/2 support:
   ```python
   from .base import http_client

   async def myBrokerTrade(side, qty, ticker, price):
       response = await http_client.post(url, json=data, headers=headers)
       # Automatically uses connection pooling (20 keepalive, 100 max connections)
   ```

4. **Session Caching**: Cache static data (profiles, account lists) during session initialization to avoid redundant API calls:
   ```python
   async def get_mybroker_session(session_manager):
       if "mybroker" not in session_manager._initialized:
           # Fetch once and cache in session
           accounts = await fetch_account_list()
           session_manager.sessions["mybroker"] = {
               "token": token,
               "account_ids": accounts  # Cache for reuse
           }
       return session_manager.sessions.get("mybroker")
   ```

5. **API Response Caching**: Use `api_cache` from `src/brokers/base.py` for frequently-accessed static data:
   ```python
   from .base import api_cache

   # Check cache first
   cached_data = api_cache.get(f"mybroker_profile_{user_id}")
   if cached_data:
       return cached_data

   # Fetch and cache
   data = await fetch_profile()
   api_cache.set(f"mybroker_profile_{user_id}", data)  # TTL: 5 minutes
   ```

## Development Commands

### Setup and Installation
```bash
# Install dependencies (uv owns the lockfile; there is no requirements.txt)
uv sync

# Configure credentials
./stockshotgun setup
```

Package layout is src-based (`[tool.setuptools.package-dir] "" = "src"`), so
`brokers`, `cli`, `execution`, `enforcement`, `agentic`, `tui`, and `signals` are
top-level importable modules rooted at `src/`. The `./stockshotgun` shim prepends
`src/` to `sys.path`; invoking modules directly needs `PYTHONPATH=src`.

### Running the Application
```bash
# TUI mode (interactive)
./stockshotgun

# CLI mode - buy/sell orders
./stockshotgun buy 10 TSLA              # Market order to all configured brokers
./stockshotgun sell 5 AAPL 175.50       # Limit order to all configured brokers
./stockshotgun buy 10 TSLA --broker Fennel --broker Public  # Specific brokers

# View holdings
./stockshotgun holdings TSLA --broker Fennel

# Scan the Nasdaq splits calendar for reverse-split signals, then list what's staged
./stockshotgun signals scan
./stockshotgun signals list --status new

# Aggregate JSON snapshot of RSA state (trades + positions + signal counts) for Pulse polling
./stockshotgun status

# Equivalent without the shim
python3 src/main.py buy 10 TSLA
```

### Agentic / MCP surfaces
```bash
# JSON agent CLI (propose → execute), MCP router, single-broker MCP server
PYTHONPATH=src uv run python -m agentic.cli --json list-brokers
PYTHONPATH=src uv run python -m agentic.router
PYTHONPATH=src uv run python -m agentic.broker Fennel
```

### Type Checking and Linting
```bash
# Full gate: mypy + py_compile + smoke tests
scripts/verify.sh

# Individually
uv run --python 3.14 mypy . --show-error-codes --pretty --ignore-missing-imports
uv run --python 3.14 python -m py_compile stockshotgun src/main.py src/setup.py \
    src/order_processor.py src/brokers/*.py src/tui/*.py
```

### Tests
```bash
# Use the project venv — system python3 lacks h2 and other deps
.venv/bin/python -m pytest -q
```

## Adding a New Broker

### Standard Pattern (API-based brokers)

1. **Create broker module** in `src/brokers/{broker}.py` with:
   - `{broker}Trade(side, qty, ticker, price)` - async function
   - `{broker}GetHoldings(ticker=None)` - async function
   - `get_{broker}_session(session_manager)` - session initialization
   - **IMPORTANT**: Wrap all blocking SDK calls with `await asyncio.to_thread()` (see Concurrency Patterns above)
   - **IMPORTANT**: Add `await rate_limiter.wait_if_needed("BrokerName")` at the start of trade/holdings functions
   - Use shared `http_client` from `src/brokers/base.py` instead of creating new HTTP clients
   - Cache static data (account IDs, profiles) in session initialization

### Browser Automation Pattern (Wells Fargo)

For brokers requiring browser automation (like Wells Fargo), use a class-based encapsulation pattern:

1. **Create a client class** in `src/brokers/{broker}.py`:
   - Encapsulate browser state, authentication, and operations in a class
   - Implement async context manager (`__aenter__`, `__aexit__`) for automatic cleanup
   - Use lazy authentication (browser created on first operation)
   - Cache browser session across multiple operations within the same instance

   Example structure:
   ```python
   class WellsFargoClient:
       def __init__(self, username, password, phone_suffix="", headless=True):
           self._username = username
           self._password = password
           self._browser = None  # Lazy initialization
           self._page = None
           self._is_authenticated = False

       async def __aenter__(self):
           return self

       async def __aexit__(self, exc_type, exc_val, exc_tb):
           if self._browser:
               await self._browser.stop()

       async def _ensure_authenticated(self):
           # Lazy auth: only create browser when needed
           if not self._is_authenticated:
               await self._authenticate()

       async def get_holdings(self, ticker=None):
           await self._ensure_authenticated()
           # Implementation...

       async def trade(self, side, qty, ticker, price):
           await self._ensure_authenticated()
           # Implementation...
   ```

2. **Wrapper functions for compatibility**:
   ```python
   async def wellsfargoGetHoldings(ticker=None):
       await rate_limiter.wait_if_needed("WellsFargo")
       session = await session_manager.get_session("WellsFargo")

       headless = os.getenv("HEADLESS", "true").lower() == "true"
       async with WellsFargoClient(
           username=session["username"],
           password=session["password"],
           phone_suffix=session.get("phone_suffix", ""),
           headless=headless
       ) as client:
           return await client.get_holdings(ticker)
   ```

3. **Browser automation best practices**:
   - Set `self._page` early in authentication flow to prevent `None` errors
   - Add clear user prompts for anti-bot challenges/CAPTCHAs
   - Verify both URL and page title for successful login (not just one)
   - Handle re-authentication after puzzle solving
   - Use comprehensive debugging (can be commented out later)
   - Proper error handling with browser cleanup in exception handlers

2. **Update `src/brokers/registry.py`** (single source of truth — ADR 0004):
   - Add ONE `BrokerSpec(...)` to the `_SPECS` tuple: `name`, `session_key`,
     `env_vars`, lazy `"module:symbol"` refs for `trade` / `holdings` /
     `validate` (optional) / `session_getter`, and flags (`requires_mfa`,
     `supports_fractional`, `multi_account`, `enabled`, `notes`).
   - `BrokerConfig.BROKERS`, `session_manager`, the CLI/TUI function maps, and
     the agentic router's spec loader all derive from this automatically — no
     other registration edits.

3. **Update `src/brokers/base.py`**:
   - Add rate limit to `RateLimiter.BROKER_LIMITS` dict (requests per second).
     Rate limits are not yet part of the registry.

4. **Update `src/setup.py`**:
   - Add broker credentials to the `brokers` dict with env_vars and prompts

The agentic MCP server for the new broker needs no new files — the generic
`python -m agentic.broker <Name>` entrypoint serves any broker in the registry.

## Supported Brokers

The following 13 brokers are currently integrated and enabled (canonical order = `_SPECS` order in `src/brokers/registry.py`, which is also the fan-out order):

| Broker | Auth Method | Required Env Vars | MFA/Special Notes |
|--------|-------------|-------------------|-------------------|
| **Robinhood** | Username/Password | `ROBINHOOD_USER`, `ROBINHOOD_PASS`, `ROBINHOOD_MFA` | Requires MFA code |
| **Tradier** | API Token | `TRADIER_ACCESS_TOKEN` | Simple token auth |
| **TastyTrade** | OAuth 2.0 | `TASTY_CLIENT_ID`, `TASTY_CLIENT_SECRET`, `TASTY_REFRESH_TOKEN` | SDK-based (`tastytrade`) |
| **Public** | API Token | `PUBLIC_API_SECRET` | Simple token auth |
| **Firstrade** | Username/Password | `FIRSTRADE_USER`, `FIRSTRADE_PASS`, `FIRSTRADE_MFA` | Requires MFA code |
| **Fennel** | Personal Access Token | `FENNEL_ACCESS_TOKEN` | Get from Fennel dashboard; only broker with `multi_account` + `account_scoped_trade` (one gated leg per live order) |
| **Schwab** | OAuth 2.0 | `SCHWAB_API_KEY`, `SCHWAB_API_SECRET`, `SCHWAB_CALLBACK_URL`, `SCHWAB_TOKEN_PATH` | Token cached in `tokens/` |
| **BBAE** | Username/Password | `BBAE_USER`, `BBAE_PASS` | May require CAPTCHA/OTP |
| **DSPAC** | Username/Password | `DSPAC_USER`, `DSPAC_PASS` | May require CAPTCHA/OTP |
| **SoFi** | Username/Password | `SOFI_USER`, `SOFI_PASS`, optional: `SOFI_TOTP` | `curl_cffi` with `impersonate="chrome"` (TLS fingerprint), no SDK |
| **Webull** | Pre-obtained credentials | `WEBULL_PROFILES` (JSON, multi-profile) | Chrome extension required (see Notes); configure via `./stockshotgun setup` |
| **Wells Fargo** | Browser automation | `WELLSFARGO_USER`, `WELLSFARGO_PASS`, optional: `WELLSFARGO_PHONE_SUFFIX` | Zendriver; may need manual CAPTCHA |
| **Chase** | Browser automation | `CHASE_USER`, `CHASE_PASS` | Zendriver via `browser_utils`; `ChaseClient` class pattern |

All 13 brokers support both `Trade` and `GetHoldings`. Nine also expose a `Validate`
function (all but Public, Fennel, Wells Fargo, and Chase) — `resolve_validate()`
returns `None` for those, and `validate_targets(...)` skips them.

## Common Patterns

### Complete Broker Function Template
```python
import asyncio
from .base import http_client, rate_limiter
from .session_manager import session_manager

async def myBrokerTrade(side, qty, ticker, price):
    """Execute a trade on MyBroker."""
    # Step 1: Rate limiting (ALWAYS FIRST)
    await rate_limiter.wait_if_needed("MyBroker")

    # Step 2: Get session
    session = await session_manager.get_session("MyBroker")
    if not session:
        print("No MyBroker credentials supplied, skipping")
        return None

    # Step 3: Extract cached data from session
    token = session.get("token")
    account_ids = session.get("account_ids", [])

    # Step 4: Wrap blocking SDK calls
    try:
        # For synchronous SDK calls, use asyncio.to_thread
        result = await asyncio.to_thread(
            broker_sdk.place_order,
            ticker=ticker,
            quantity=qty,
            side=side
        )

        # For async HTTP calls, use shared client
        response = await http_client.post(
            url,
            json={"order": "data"},
            headers={"Authorization": f"Bearer {token}"}
        )

        print(f"Order placed successfully on MyBroker")
    except Exception as e:
        print(f"Error trading {ticker} on MyBroker: {str(e)}")
        import traceback
        traceback.print_exc()
```

### Wrapping Blocking SDK Calls
```python
import asyncio

# Bad: Blocks event loop
result = blocking_sdk_call(arg1, arg2)

# Good: Runs in thread pool
result = await asyncio.to_thread(blocking_sdk_call, arg1, arg2)

# For methods
result = await asyncio.to_thread(object.method, arg1, kwarg=value)
```

### Session Initialization with Caching
```python
async def get_mybroker_session(session_manager):
    """Get or create MyBroker session with cached account data."""
    if "mybroker" not in session_manager._initialized:
        TOKEN = os.getenv("MYBROKER_TOKEN")

        if not TOKEN:
            session_manager.sessions["mybroker"] = None
        else:
            # Fetch and cache account IDs once
            account_ids = await fetch_accounts(TOKEN)

            session_manager.sessions["mybroker"] = {
                "token": TOKEN,
                "account_ids": account_ids  # Cached for reuse
            }
            print(f"✓ MyBroker initialized ({len(account_ids)} accounts)")

        session_manager._initialized.add("mybroker")

    return session_manager.sessions.get("mybroker")
```

## Important Files and Locations

- `.env` - Credentials (NEVER commit, in .gitignore)
- `tokens/` - OAuth tokens for brokers like Schwab
- `.venv/` - Virtual environment (`uv sync` creates it; use `.venv/bin/python` for tests)
- `pyproject.toml` + `uv.lock` - Python dependencies (no `requirements.txt`)
- `stockshotgun` - Root launcher shim that puts `src/` on `sys.path`
- `scripts/verify.sh` - mypy + py_compile + smoke-test gate

### Environment Variables

The application uses environment variables for configuration, stored in `.env` file:

**Global Settings:**
- `HEADLESS` - Browser headless mode for the zendriver brokers. `browser_utils.create_browser` defaults to `true`, but the Wells Fargo and Chase wrappers read it with a `false` default — so those two run headed unless `HEADLESS=true`
- `BROWSER_PATH` - Override the Chrome/Chromium binary zendriver launches
- `DRY_RUN` - Chase-side dry-run switch (order preview, no submit)

**Broker Credentials:** (see Supported Brokers table for complete list)
- Each broker requires specific environment variables for authentication
- Use `./stockshotgun setup` to interactively configure credentials
- Never commit `.env` file - it's in `.gitignore`

### Key Dependencies

Pinned in `pyproject.toml` (source of truth — update there, not here):

- **urwid** (3.0.5) - Terminal UI framework for the TUI mode
- **python-dotenv** (1.2.1) - Environment variable management from `.env` file
- **pyotp** (2.9.0) - One-time password (MFA/2FA) support
- **httpx[http2]** (0.28.1) - Modern async HTTP client with HTTP/2 support
- **zendriver** (0.15.2) - Browser automation for Wells Fargo and Chase (Chrome DevTools Protocol)
- **selectolax** (>=0.4.7) - HTML parsing (replaced beautifulsoup4)
- **curl-cffi** (0.14.0) - HTTP client with TLS fingerprint spoofing (SoFi)
- **mcp** (>=1.27.1) - MCP server/client for the agentic router and per-broker servers

Broker-specific SDKs:
- **tastytrade** (12.0.2) - TastyTrade API client
- **firstrade** (0.0.38) - Firstrade API client
- **schwab-py** (1.5.1) - Schwab OAuth and trading API
- **robin-stocks** (3.4.0) - Robinhood API wrapper
- **bbae-invest-api** (0.1.5) - BBAE broker API
- **dspac-invest-api** (0.1.4) - DSPAC broker API
- **webull** (git pin) - Webull API (custom fork for api_login support)

## Notes

- The project requires Python 3.14+ (`requires-python = ">=3.14"`) and uses async/await throughout
- TUI is built with urwid library for terminal interfaces
- Each broker may have different authentication methods (API keys, username/password, OAuth, browser automation)

### Broker-Specific Notes

- **Fennel**: Uses personal access tokens from their dashboard, not email/password authentication
- **BBAE/DSPAC**: Both are Redbridge Securities brokers sharing one implementation via `src/brokers/_redbridge_broker.py`'s `make_redbridge_broker` factory — fix one and both get it. May require CAPTCHA or OTP codes during initial login
- **Schwab**: Uses OAuth with token persistence in `tokens/` directory
- **Webull**: Due to Webull API changes (Sept 2025), traditional username/password login is broken. Instead, use pre-obtained credentials from a browser session:
  1. Install Chrome extension: https://github.com/ImNotOssy/webull/releases/tag/1
  2. Login to Webull in Chrome with extension active
  3. Extension captures credentials (access_token, refresh_token, uuid, account_id)
  4. Run `./stockshotgun setup` to write them into `WEBULL_PROFILES` (a JSON array — one entry per profile, each with its own accounts and optional `trading_pin`)
  5. The integration uses `api_login()` instead of traditional login
  6. Trades are skipped for any profile with no trade token — add `trading_pin` to that profile
  7. Background: https://github.com/tedchou12/webull/issues/456
- **Wells Fargo**: Uses browser automation (Zendriver) instead of API:
  - Requires `WELLSFARGO_USER` and `WELLSFARGO_PASS` environment variables
  - Optional: `WELLSFARGO_PHONE_SUFFIX` for MFA (last 4 digits of phone)
  - Supports headless mode via `HEADLESS=true/false` environment variable (default: true)
  - May require manual anti-bot/CAPTCHA solving in non-headless mode
  - Implements class-based pattern with `WellsFargoClient` for state management
  - Automatically discovers multiple accounts (WELLSTRADE, IRAs) from single login
  - Browser session is cached within a single client instance for efficiency
- **Chase**: Also zendriver-based, built on the shared `src/brokers/browser_utils.py` helpers:
  - Requires `CHASE_USER` and `CHASE_PASS`
  - `ChaseClient` class pattern mirroring Wells Fargo; runs headed unless `HEADLESS=true`
  - `DRY_RUN=true` previews orders without submitting
  - Order status is read from Chase's own codes (`FULLY_EXECUTED` / `PARTIALLY_EXECUTED` = filled)

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`DrCrinkle/StockShotGun`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default label vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout — `CONTEXT.md` at root + `docs/adr/`. See `docs/agents/domain.md`.
