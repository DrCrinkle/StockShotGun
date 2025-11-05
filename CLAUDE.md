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

2. **Update `brokers/base.py`**:
   - Add broker entry to `BrokerConfig.BROKERS` with session_key, env_vars, requires_mfa, enabled

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

### Accessing Session Manager
```python
from .session_manager import session_manager

async def myBrokerTrade(side, qty, ticker, price):
    session = await session_manager.get_session("MyBroker")
    if not session:
        print("No MyBroker credentials supplied, skipping")
        return None
    # Use session data for trading
```

### Using Shared HTTP Client
```python
from .base import http_client, rate_limiter

await rate_limiter.wait_if_needed("MyBroker")
response = await http_client.post(url, json=data, headers=headers)
```

### Error Handling in Broker Functions
```python
import traceback

try:
    # Broker API call
    pass
except Exception as e:
    print(f"Error trading {ticker} on MyBroker: {str(e)}")
    traceback.print_exc()
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
