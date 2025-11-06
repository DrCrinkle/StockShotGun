"""Response handling for the TUI."""

import time
from .config import MAX_RESPONSE_HISTORY


class ResponseWriter:
    """Writer class that redirects stdout/stderr to the TUI response box."""

    def __init__(self, add_response_fn):
        self.add_response = add_response_fn
        self.pending_redraw = False
        self.last_redraw_time = 0
        self.redraw_debounce_ms = 100  # Debounce redraws by 100ms

    def write(self, text):
        if text.strip():  # Only process non-empty strings
            self.add_response(text.rstrip(), force_redraw=self._should_redraw())

    def flush(self):
        pass

    def _should_redraw(self):
        """Check if enough time has passed to allow a redraw."""
        current_time = time.time() * 1000  # Convert to milliseconds
        if current_time - self.last_redraw_time > self.redraw_debounce_ms:
            self.last_redraw_time = current_time
            return True
        return False


class MemoryEfficientResponseStorage:
    """Memory-efficient storage for response history."""

    def __init__(self, max_items=MAX_RESPONSE_HISTORY):
        self.max_items = max_items
        self._items = []
        self._total_chars = 0
        self._max_chars = 100000  # Limit total characters to prevent memory issues

    def add_response(self, text):
        """Add a response with memory management."""
        # Estimate memory usage (rough approximation)
        estimated_chars = len(str(text))

        # If adding this would exceed limits, remove oldest items
        while (len(self._items) >= self.max_items or
               (self._total_chars + estimated_chars > self._max_chars and self._items)):
            removed_item = self._items.pop(0)
            self._total_chars -= len(str(removed_item))

        # Add new item
        self._items.append(text)
        self._total_chars += estimated_chars

    def get_items(self):
        """Get all stored items."""
        return self._items.copy()

    def clear(self):
        """Clear all stored items."""
        self._items.clear()
        self._total_chars = 0

    def __len__(self):
        return len(self._items)
