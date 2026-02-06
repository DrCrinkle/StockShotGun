"""Response handling for the TUI."""

import time
import os
from order_processor import current_broker
from tui.config import MAX_RESPONSE_HISTORY


class ResponseWriter:
    """Writer class that redirects stdout/stderr to the TUI response box."""

    def __init__(self, add_response_fn):
        self.add_response = add_response_fn
        self.pending_redraw = False
        self.last_redraw_time = 0
        self.redraw_debounce_ms = 100  # Debounce redraws by 100ms
        self.verbosity = "normal"
        self.set_verbosity(os.getenv("SSG_LOG_VERBOSITY", "normal"))
        self._recent_lines = {}
        self._repeat_window_s = 3.0

    def set_verbosity(self, level):
        normalized = str(level).strip().lower()
        if normalized not in {"quiet", "normal", "verbose"}:
            normalized = "normal"
        self.verbosity = normalized
        return self.verbosity

    def cycle_verbosity(self):
        levels = ["quiet", "normal", "verbose"]
        current_index = levels.index(self.verbosity)
        self.verbosity = levels[(current_index + 1) % len(levels)]
        return self.verbosity

    def _is_repeated(self, line):
        now = time.time()
        last_seen = self._recent_lines.get(line)
        self._recent_lines[line] = now

        stale_before = now - self._repeat_window_s
        self._recent_lines = {
            k: v for k, v in self._recent_lines.items() if v >= stale_before
        }

        return last_seen is not None and (now - last_seen) < self._repeat_window_s

    def _is_low_signal(self, line):
        lowered = line.lower()
        noisy_markers = (
            "debug",
            "waiting for",
            "current url",
            "page title",
            "api response",
            "page text",
            "attempt ",
        )
        return any(marker in lowered for marker in noisy_markers)

    def _should_emit(self, line):
        if not line:
            return False

        if self._is_repeated(line):
            return False

        if self.verbosity == "verbose":
            return True

        if self.verbosity == "quiet":
            important = ("❌", "⚠", "✅", "✗", "Error", "Failed", "success")
            return any(marker in line for marker in important)

        return not self._is_low_signal(line)

    def write(self, text):
        broker = current_broker.get()
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if self._should_emit(line):
                if broker:
                    line = f"[{broker}] {line}"
                self.add_response(line, force_redraw=self._should_redraw())

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
        while len(self._items) >= self.max_items or (
            self._total_chars + estimated_chars > self._max_chars and self._items
        ):
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
