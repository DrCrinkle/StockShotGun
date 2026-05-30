"""
TUI package for StockShotGun.

This package contains modular components for the terminal user interface.
"""

# Export all components for easy importing
from tui.config import (
    MAX_RESPONSE_HISTORY,
    RESPONSE_BOX_HEIGHT,
    MODAL_WIDTH,
    MODAL_HEIGHT,
    BROKERS,
    PALETTE,
)

from tui.widgets import (
    EditWithCallback,
    AsyncioEventLoop,
)

from tui.input_handler import (
    TUIInputHandler,
    TUICompatibleInput,
    tui_input_handler,
    tui_compatible_input,
    set_non_interactive_mode,
    setup_tui_input_interception,
    restore_original_input,
)

from tui.response_handler import (
    ResponseWriter,
    MemoryEfficientResponseStorage,
)

from tui.session_cache import (
    SessionStatusCache,
    session_cache,
)

from tui.holdings_view import (
    HoldingsView,
)

from brokers.registry import (
    broker_functions_map as _broker_functions_map,
    get_broker_function,
)

# Per-broker functions derived from the broker registry (ADR 0004; the old
# tui.broker_functions module was removed).
BROKER_FUNC_CONFIG = _broker_functions_map()

# Import run_tui from app module
from tui.app import run_tui

__all__ = [
    # Config
    "MAX_RESPONSE_HISTORY",
    "RESPONSE_BOX_HEIGHT",
    "MODAL_WIDTH",
    "MODAL_HEIGHT",
    "BROKERS",
    "PALETTE",
    # Widgets
    "EditWithCallback",
    "AsyncioEventLoop",
    # Input handling
    "TUIInputHandler",
    "TUICompatibleInput",
    "tui_input_handler",
    "tui_compatible_input",
    "set_non_interactive_mode",
    "setup_tui_input_interception",
    "restore_original_input",
    # Response handling
    "ResponseWriter",
    "MemoryEfficientResponseStorage",
    # Session cache
    "SessionStatusCache",
    "session_cache",
    # Holdings view
    "HoldingsView",
    # Broker functions
    "BROKER_FUNC_CONFIG",
    "get_broker_function",
    # Main app
    "run_tui",
]
