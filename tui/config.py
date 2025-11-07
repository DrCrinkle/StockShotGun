"""Configuration and constants for the TUI."""

from brokers import BrokerConfig

# Constants for configuration
MAX_RESPONSE_HISTORY = 250  # Maximum number of response items to keep
RESPONSE_BOX_HEIGHT = 10     # Height of the response box
MODAL_WIDTH = 60             # Width of modal dialogs
MODAL_HEIGHT = 12            # Height of modal dialogs

# Get list of enabled brokers from centralized configuration
BROKERS = BrokerConfig.get_all_brokers()

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
