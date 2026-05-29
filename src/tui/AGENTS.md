# TUI LAYER KNOWLEDGE BASE

**Location:** `src/tui/`
**Framework:** urwid 3.0.3 (async-enabled)

## OVERVIEW
Interactive terminal interface for concurrent multi-broker order execution and holdings management.
Root AGENTS.md conventions apply; this file documents TUI-only event-loop and input-bridge behavior.

## STRUCTURE
```
src/tui/
├── app.py              # Main loop, frame assembly, state orchestration (731 lines)
├── input_handler.py    # CRITICAL: MFA/sync input interception via modal dialogs
├── broker_functions.py # BROKER_CONFIG: maps broker names → trade/holdings/validate fns
├── config.py           # Constants (palette, modal dims) + BROKERS list from BrokerConfig
├── session_cache.py    # 5s TTL cache for broker auth status
├── response_handler.py # stdout/stderr → ResponseBox widget with 100ms debounced redraws
├── widgets.py          # Custom urwid: EditWithCallback, ResponseBox (215 lines)
├── holdings_view.py    # Tabular position display
└── __init__.py         # Exports run_tui()
```

## KEY COMPONENTS

**MFA Interception (`input_handler.py`):**
- Hijacks `builtins.input` via `setup_tui_input_interception()`
- Brokers calling `input()` trigger a modal TUI dialog instead of CLI prompt
- **Sync/Async Bridge**: Uses `asyncio.Future` + manual `event_loop._run_once()` pumping to keep UI responsive while blocking caller
- Resilient exception handler prevents background broker errors from crashing TUI
- Supports `--non-interactive` mode: raises `CliRuntimeError(NON_INTERACTIVE_INPUT_REQUIRED)` instead of prompting

**Response Redirection (`response_handler.py`):**
- `ResponseWriter` captures all `print()` and log output
- Routes to `ResponseBox` widget with debounced redraws (100ms)
- Prevents flickering during high-volume concurrent order updates

**Broker Registry (`broker_functions.py`):**
- `BROKER_CONFIG` dict maps broker names → `{trade, holdings, validate}` function refs
- `get_broker_function(name, type)` checks `BrokerConfig.enabled` before returning
- This is 1 of 5 registration points for new brokers (see src/brokers/AGENTS.md)

**Session Management (`session_cache.py`):**
- 5-second TTL cache for broker connection status
- UI queries this cache instead of triggering expensive session validation

## ANTI-PATTERNS (TUI-SPECIFIC)
- **`time.sleep()` / `threading.Event.wait()`**: Freezes entire interface. Use `asyncio.create_task()` instead
- **`asyncio.Event.wait()` in input_handler**: Causes MFA deadlocks. Must use `_run_once()` pumping
- **`loop.draw_screen()` in tight loop**: Use `ResponseWriter` debounced updates
- **Direct `input()` in broker code**: Already intercepted — just call `input()` normally and the TUI modal handles it

## EVENT LOOP RULES
1. **Never block**: No synchronous waits of any kind
2. **Background tasks**: `asyncio.create_task()` for all broker operations
3. **Manual pumping**: ONLY `TUIInputHandler.prompt_user` may pump `_run_once()` (sync-to-async bridge for MFA)
4. **Draw sparingly**: Let event loop handle natural redraws; force only for modals or critical updates
5. **Robinhood output**: `app.py` redirects `robin_stocks` output via `set_robinhood_output` to prevent console spam
