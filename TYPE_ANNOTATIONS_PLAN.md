# Type Annotations Plan for StockShotGun

## Overview
Add comprehensive type annotations to the StockShotGun codebase to improve maintainability, IDE support, and catch potential bugs at static analysis time.

**Current State**: 6 of 28 Python files have partial typing. ~200+ mypy errors with strict checking.
**Target State**: Full type coverage with passing mypy checks.

### Approach: Gradual Typing
- Start with basic annotations (return types, parameter types)
- Use `ignore_missing_imports = true` for third-party libraries
- Enable stricter options incrementally after base coverage is complete
- Create shared `types.py` with TypedDict definitions for reusability

---

## Phase 1: Foundation Setup

### 1.1 Create shared types module
**File**: `types.py` (new file in project root)

```python
from typing import Any, Callable, Dict, List, Optional, TypedDict

# Session types
class SessionData(TypedDict, total=False):
    token: str
    access_token: str
    account_ids: List[str]
    username: str
    password: str

# Order types
class Order(TypedDict):
    action: str  # "buy" | "sell"
    quantity: int
    ticker: str
    price: Optional[float]
    selected_brokers: List[str]

# Holdings types
class Position(TypedDict, total=False):
    symbol: str
    quantity: float
    cost_basis: Optional[float]
    current_value: Optional[float]

Holdings = Dict[str, List[Position]]

# Result types
class OrderResult(TypedDict):
    successful: int
    failed: int
    skipped: int
    status: Dict[str, List[str]]

# Function signatures
TradeFunction = Callable[[str, int, str, Optional[float]], Optional[bool]]
HoldingsFunction = Callable[[Optional[str]], Optional[Holdings]]
ResponseCallback = Callable[[str, Optional[str], bool], None]
```

### 1.2 Add mypy configuration
**File**: `pyproject.toml` (append to existing)

```toml
[tool.mypy]
python_version = "3.13"
warn_return_any = true
warn_unused_ignores = true
check_untyped_defs = true
ignore_missing_imports = true
# Gradually enable strict mode:
# disallow_untyped_defs = true
```

### 1.3 Create py.typed marker
**File**: `py.typed` (empty file in project root)

---

## Phase 2: Core Infrastructure (4 files)

### 2.1 `brokers/base.py` (311 LOC) - Already 50% typed
- Add return types to `RateLimiter.__init__`, `wait_if_needed`
- Add return types to `APICache.__init__`, `get`, `set`, `clear`
- Type `retry_operation()` with generic `T` for return value
- Type `_login_broker()` and `_get_broker_holdings()`

### 2.2 `brokers/session_manager.py` (149 LOC)
- Type `BrokerSessionManager.__init__`
- Type `get_session()` -> `Optional[SessionData]`
- Type `initialize_brokers()` -> `None`
- Type all internal methods

### 2.3 `order_processor.py` (critical - 0% typed)
- Type `OrderBatchProcessor.__init__`
- Type `process_orders()` with proper return type
- Type `_process_batch()` and `_process_single_order()`
- Fix `asyncio.gather` result handling (current bug source)

### 2.4 `main.py` (157 LOC)
- Type `main()` -> `None`
- Type CLI argument handling

---

## Phase 3: Broker Modules (14 files)

### Standard broker function signatures:
```python
async def {broker}Trade(
    side: str,
    qty: int,
    ticker: str,
    price: float | None
) -> bool | None:
    """Returns True=success, False=failure, None=skipped"""

async def {broker}GetHoldings(
    ticker: str | None = None
) -> Holdings | None:
    """Returns holdings dict or None if unavailable"""

async def get_{broker}_session(
    session_manager: BrokerSessionManager
) -> SessionData | None:
    """Returns session data or None if no credentials"""
```

### Files to update (in order):
1. `brokers/fennel.py` (199 LOC) - simple, good template
2. `brokers/tradier.py` (206 LOC) - simple API
3. `brokers/bbae.py` (116 LOC) - simple
4. `brokers/dspac.py` (116 LOC) - same as BBAE
5. `brokers/firstrade.py` (175 LOC) - SDK-based
6. `brokers/tastytrade.py` (135 LOC) - SDK-based
7. `brokers/schwab.py` (180 LOC) - OAuth complexity
8. `brokers/public.py` (381 LOC) - larger file
9. `brokers/robinhood.py` (255 LOC) - already partial
10. `brokers/sofi.py` (455 LOC) - browser automation
11. `brokers/webull.py` (419 LOC) - complex API
12. `brokers/wellsfargo.py` (1049 LOC) - most complex, browser automation

### Special attention for wellsfargo.py:
- Fix `self._page: Page | None` type and add guards
- Type `WellsFargoClient` class fully
- Handle Optional browser/page attributes properly

---

## Phase 4: TUI Components (9 files)

### 4.1 Core TUI (high priority)
- `tui/app.py` (445 LOC) - main TUI, state management
- `tui/widgets.py` (90 LOC) - custom urwid widgets
- `tui/broker_functions.py` (96 LOC) - function mappings

### 4.2 Supporting modules
- `tui/input_handler.py` (164 LOC)
- `tui/response_handler.py` (66 LOC)
- `tui/holdings_view.py` (54 LOC)
- `tui/session_cache.py` (40 LOC)
- `tui/config.py` (23 LOC)
- `tui/__init__.py` (84 LOC)

### Note on urwid:
urwid lacks type stubs. Options:
1. Use `# type: ignore` for urwid imports
2. Create minimal local stubs in `stubs/urwid.pyi`
3. Accept partial typing for TUI layer

---

## Phase 5: Utilities & Cleanup

### 5.1 Remaining files
- `setup.py` (133 LOC) - credential setup wizard
- `brokers/__init__.py` (94 LOC) - exports

### 5.2 Final validation
```bash
mypy . --show-error-codes --pretty --ignore-missing-imports
mypy . --strict --ignore-missing-imports  # Target for future
```

---

## Estimated Scope

| Phase | Files | Est. Changes | Priority |
|-------|-------|--------------|----------|
| 1. Foundation | 3 new/modified | ~80 lines | HIGH |
| 2. Core | 4 files | ~150 lines | HIGH |
| 3. Brokers | 14 files | ~400 lines | MEDIUM |
| 4. TUI | 9 files | ~200 lines | LOW |
| 5. Utilities | 2 files | ~50 lines | LOW |

**Total estimated**: ~880 lines of type annotation additions across 28 files.

---

## Critical Files (must modify)

1. `types.py` (NEW)
2. `pyproject.toml` (add mypy config)
3. `brokers/base.py`
4. `brokers/session_manager.py`
5. `order_processor.py`
6. `main.py`
7. All 14 broker modules
8. `tui/app.py`
9. `tui/broker_functions.py`

---

## Success Criteria

1. `mypy . --ignore-missing-imports` passes with 0 errors
2. All public functions have type annotations
3. All async functions have return type annotations
4. TypedDict used for structured data (Orders, Holdings, Sessions)
5. IDE autocomplete works throughout codebase
