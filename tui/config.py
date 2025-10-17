"""Configuration and constants for the TUI."""

import concurrent.futures

# Constants for configuration
MAX_RESPONSE_HISTORY = 250  # Maximum number of response items to keep
RESPONSE_BOX_HEIGHT = 10     # Height of the response box
MODAL_WIDTH = 60             # Width of modal dialogs
MODAL_HEIGHT = 12            # Height of modal dialogs
REDRAW_DELAY = 0.01          # Seconds between redraws during input waiting

# Broker configuration
BROKER_CONFIG = {
    "Robinhood": {
        "enabled": True
    },
    "Tradier": {
        "enabled": True
    },
    "TastyTrade": {
        "enabled": True
    },
    "Public": {
        "enabled": True
    },
    "Firstrade": {
        "enabled": True
    },
    "Fennel": {
        "enabled": True
    },
    "Schwab": {
        "enabled": True
    },
    "BBAE": {
        "enabled": True
    },
    "DSPAC": {
        "enabled": True
    },
    "SoFi": {
        "enabled": True
    },
}

# Get list of enabled brokers
BROKERS = [name for name, config in BROKER_CONFIG.items() if config["enabled"]]

# Global thread pool for efficient broker task execution
broker_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=12,  # Support up to 12 concurrent broker operations
    thread_name_prefix="broker_worker"
)

# Thread pool statistics for monitoring
thread_pool_stats = {
    "tasks_submitted": 0,
    "tasks_completed": 0,
    "tasks_failed": 0
}

# Color palette for urwid
PALETTE = [
    ("reversed", "standout", ""),
    ("editcp", "light gray", "dark blue"),
    ("success", "light green", ""),
    ("warning", "yellow", ""),
    ("error", "light red", ""),
    ("info", "light cyan", ""),
    ("header", "white", "dark blue"),
]
