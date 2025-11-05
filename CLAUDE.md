# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

StockShotGun is a multi-broker trading application that allows submitting orders to multiple brokerage accounts simultaneously. It's designed for reverse split arbitrage trading where speed and coordination across multiple brokers is essential.

The application supports both CLI and TUI (Terminal User Interface) modes for flexibility in different use cases.

## Architecture

### Core Components

**brokers/** - Modular broker integrations
- Each broker has its own module (e.g., `fennel.py`, `schwab.py`, `robinhood.py`)
- All broker modules follow a consistent pattern with two main functions:
  - `{broker}Trade(side, qty, ticker, price)` - Execute trades
  - `{broker}GetHoldings(ticker=None)` - Retrieve holdings
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

**tui/** - Terminal User Interface components
- `app.py` - Main TUI application entry point with `run_tui()` function
- `broker_functions.py` - Maps broker names to their trade/holdings functions
- `holdings_view.py` - Display broker holdings in the TUI
- `response_handler.py` - Manages broker response display
- `input_handler.py` - Intercepts Python's `input()` for TUI compatibility
- `session_cache.py` - Caches session status to avoid redundant initialization
- `widgets.py` - Custom urwid widgets for the TUI

**main.py** - Application entry point
- If arguments provided → CLI mode
- If no arguments → TUI mode
- Handles broker selection via `--broker` flag or defaults to all configured brokers
- Uses `order_processor` for concurrent order execution

**order_processor.py** - Shared order processing (used by both CLI and TUI)
- `OrderBatchProcessor` - Processes orders in batches with concurrent broker execution
- Handles error reporting per broker
- Provides progress tracking and execution summaries

**setup.py** - Interactive credential setup wizard
- Validates existing credentials before prompting for new ones
- Stores credentials in `.env` file (never commit this file)

### Key Design Patterns

1. **Async-First Architecture**: All broker operations use `asyncio` for concurrent execution. The `OrderBatchProcessor` handles concurrent execution across multiple brokers, ensuring trades are submitted simultaneously rather than sequentially.

2. **Centralized Order Processing**: Both CLI and TUI modes use `order_processor.OrderBatchProcessor` for order execution. This provides:
   - Concurrent broker execution (all brokers execute in parallel)
   - Batch processing for multiple orders
   - Consistent error handling and status reporting
   - Progress tracking across all operations

3. **Centralized Configuration**: `BrokerConfig` in `brokers/base.py` is the single source of truth for broker configuration. When adding a new broker:
   - Add broker config to `BrokerConfig.BROKERS`
   - Create broker module with trade/holdings functions
   - Add session getter to `session_manager.py`
   - Import functions in `brokers/__init__.py`
   - Add to `tui/broker_functions.py` mapping

4. **Session Management**: The `BrokerSessionManager` handles authentication and session lifecycle. Sessions are lazy-loaded and cached to minimize login overhead.

5. **Error Handling**: Individual broker failures don't halt the entire operation. The `OrderBatchProcessor` catches exceptions per broker and reports them independently, allowing successful brokers to complete while failed ones are logged.

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

   Per-broker rate limits are configured in `brokers/base.py` in the `RateLimiter.BROKER_LIMITS` dict.

3. **Shared HTTP Client**: Use the shared async client from `brokers/base.py` for connection pooling and HTTP/2 support:
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

5. **API Response Caching**: Use `api_cache` from `brokers/base.py` for frequently-accessed static data:
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
# Install dependencies
pip install -r requirements.txt

# Or using virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure credentials
python3 main.py setup
```

### Running the Application
```bash
# TUI mode (interactive)
python3 main.py

# CLI mode - buy/sell orders
python3 main.py buy 10 TSLA              # Market order to all configured brokers
python3 main.py sell 5 AAPL 175.50       # Limit order to all configured brokers
python3 main.py buy 10 TSLA --broker Fennel --broker Public  # Specific brokers

# View holdings
python3 main.py holdings TSLA --broker Fennel
```

### Testing
```bash
# Run all tests
pytest tests/ -v

# Run specific test file
pytest tests/test_broker.py -v
```

### Type Checking and Linting
```bash
# Type checking with mypy
mypy . --show-error-codes --pretty --ignore-missing-imports

# Compile check (syntax validation)
python3 -m py_compile main.py brokers/*.py tui/*.py setup.py
```

## Adding a New Broker

1. **Create broker module** in `brokers/{broker}.py` with:
   - `{broker}Trade(side, qty, ticker, price)` - async function
   - `{broker}GetHoldings(ticker=None)` - async function
   - `get_{broker}_session(session_manager)` - session initialization
   - **IMPORTANT**: Wrap all blocking SDK calls with `await asyncio.to_thread()` (see Concurrency Patterns above)
   - **IMPORTANT**: Add `await rate_limiter.wait_if_needed("BrokerName")` at the start of trade/holdings functions
   - Use shared `http_client` from `brokers/base.py` instead of creating new HTTP clients
   - Cache static data (account IDs, profiles) in session initialization

2. **Update `brokers/base.py`**:
   - Add broker entry to `BrokerConfig.BROKERS` with session_key, env_vars, requires_mfa, enabled
   - Add rate limit to `RateLimiter.BROKER_LIMITS` dict (requests per second)

3. **Update `brokers/session_manager.py`**:
   - Import the broker module
   - Add entry to `BROKER_MODULES` mapping

4. **Update `brokers/__init__.py`**:
   - Import trade and holdings functions
   - Add to `__all__` exports

5. **Update `tui/broker_functions.py`**:
   - Import broker functions
   - Add entry to `BROKER_CONFIG` dict

6. **Update `setup.py`**:
   - Add broker credentials to the `brokers` dict with env_vars and prompts

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
- `logs/` - Application logs
- `requirements.txt` - Python dependencies
- `.venv/` - Virtual environment (if using venv)

## Notes

- The project uses Python 3.13+ and async/await throughout
- TUI is built with urwid library for terminal interfaces
- Each broker may have different authentication methods (API keys, username/password, OAuth)
- Some brokers (BBAE, DSPAC) may require CAPTCHA or OTP codes during initial login
- Fennel uses personal access tokens from their dashboard, not email/password authentication
- **Webull**: Due to Webull API changes (Sept 2025), traditional username/password login is broken. Instead, use pre-obtained credentials from a browser session:
  1. Install Chrome extension: https://github.com/ImNotOssy/webull/releases/tag/1
  2. Login to Webull in Chrome with extension active
  3. Extension captures credentials (access_token, refresh_token, uuid, account_id)
  4. Add these to your .env file (supports comma-separated account IDs for multiple accounts)
  5. The integration uses `api_login()` method instead of traditional login
  6. Multiple accounts: `WEBULL_ACCOUNT_ID=12345678,87654321` or the system will auto-discover them
