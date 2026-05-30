# BROKERS KNOWLEDGE BASE

**Location:** `src/brokers/`
**Modules:** 13 broker adapters + shared infrastructure

## OVERVIEW
Multi-broker integration layer. Three implementation categories: SDK-based, REST API, and browser automation.
Root AGENTS.md conventions apply; this file captures broker-only contracts, registration, and edge cases.

## STRUCTURE
```
src/brokers/
├── registry.py          # SINGLE SOURCE OF TRUTH: BrokerSpec per broker (lazy "module:symbol" refs). ADR 0004
├── base.py              # Shared: http_client, RateLimiter, APICache, BrokerConfig (derived from registry), retry logic
├── session_manager.py   # BrokerSessionManager: lazy auth (resolves session getters from registry), per-broker locks
├── browser_utils.py     # Shared: create_browser, stop_browser, navigate_and_wait, poll_for_condition
├── __init__.py          # Exports base infra + session_manager + registry (no eager per-broker imports)
├── robinhood.py         # SDK: robin_stocks (global state, session=True boolean)
├── schwab.py            # SDK: schwab-py (OAuth, token persistence in tokens/)
├── tastytrade.py        # SDK: tastytrade (standard async)
├── firstrade.py         # SDK: firstrade (penny stock restrictions)
├── tradier.py           # REST: direct httpx via shared http_client
├── fennel.py            # REST: personal access token auth
├── public.py            # REST: API secret auth
├── bbae.py              # REST: shares _login_broker helper with DSPAC
├── dspac.py             # REST: shares _login_broker helper with BBAE
├── sofi.py              # Browser: re-authenticates every operation
├── webull.py            # Token-only: pre-obtained via Chrome extension (login broken)
├── wellsfargo.py        # Browser: WellsFargoClient class, zendriver, CAPTCHA handling (1093 lines, largest)
└── chase.py             # Browser: ChaseClient class, zendriver (1040 lines)
```

## BROKER CONTRACT
Each module MUST implement:

| Function | Signature | Returns | Required |
|----------|-----------|---------|----------|
| `{broker}Trade` | `(side, qty, ticker, price)` | `True`/`False`/`None` | Yes |
| `{broker}GetHoldings` | `(ticker=None)` | `dict[account, list[position]]` | Yes |
| `{broker}Validate` | `(side, qty, ticker, price)` | `(bool/None, error_msg)` | Most brokers |
| `get_{broker}_session` | `(session_manager)` | Session object or `None` | Yes |

**First line of every Trade/Holdings/Validate function:**
```python
await rate_limiter.wait_if_needed("BrokerName")
```

## ADDING A NEW BROKER (one registration point — ADR 0004)
1. `src/brokers/{broker}.py` — Implement Trade, GetHoldings, Validate, get_session
2. `src/brokers/registry.py` — Add ONE `BrokerSpec(...)` to the `_SPECS` tuple
   (name, session_key, env_vars, lazy `"module:symbol"` refs for
   trade/holdings/validate/session_getter, flags). This is the single source of
   truth — `BrokerConfig.BROKERS`, the session manager, the CLI/TUI function
   maps, and the agentic router all derive from it.
3. `src/brokers/base.py` — Add to `RateLimiter.BROKER_LIMITS` (rate limits are
   not yet part of the registry).

Also update `src/setup.py` if broker needs credential wizard entry. There is no
longer a per-broker `agentic/brokers/<name>/` package or a TUI broker map to
edit — the generic `python -m agentic.broker <Name>` entrypoint serves any
broker in the registry.

## IMPLEMENTATION CATEGORIES

**SDK-Based** (Robinhood, Schwab, TastyTrade, Firstrade):
- Wrap all blocking calls: `await asyncio.to_thread(sdk.method, args)`
- Cache session data (account IDs, profiles) in `session_manager.sessions`

**REST API** (Tradier, Fennel, Public, BBAE, DSPAC):
- Use shared `http_client` from `base.py` — never create new clients
- Use `api_cache.get/set` for static data (instruments, profiles)

**Browser Automation** (Wells Fargo, Chase, SoFi):
- Class with `__aenter__`/`__aexit__` for browser lifecycle
- Lazy auth: browser created on first operation only
- Use `browser_utils.create_browser()` — shared headless/user-agent/browser-path config
- Use `browser_utils.navigate_and_wait()` instead of `page.get()` + `asyncio.sleep()`
- `HEADLESS=true/false` and `BROWSER_PATH` env vars control browser behavior
- Must handle CAPTCHA/2FA interactively

## SPECIAL CASES

**Webull**: Login broken since Sept 2025. Requires pre-obtained tokens from Chrome extension. Supports comma-separated `WEBULL_ACCOUNT_ID` for multi-account. Uses `_discover_accounts` probe.

**BBAE & DSPAC**: Share `_login_broker` and `_get_broker_holdings` helpers in `base.py`. Same API backend, different credentials.

**Robinhood**: Session is global state (`robin_stocks` library). Session object is boolean `True`. Uses `asyncio.to_thread` for all SDK calls.

**Schwab**: OAuth via `schwab-py` `easy_client`. Tokens persisted in `tokens/` directory. Auto-refresh on session init.

**Chase**: Browser automation (1040 lines). Complex multi-account discovery and order flow. Holdings rewritten with correct POST APIs.

## THREAD SAFETY
- `RateLimiter`, `APICache`, `BrokerConfig` — protected by `threading.Lock` (Python 3.14 no-GIL ready)
- `BrokerSessionManager` — `threading.Lock` for shared state + `asyncio.Lock` per broker for async coordination
