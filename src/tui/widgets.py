"""Custom urwid widgets for the TUI."""

import subprocess
import urwid
from datetime import datetime


class EditWithCallback(urwid.Edit):
    """Custom Edit widget that calls a callback on every change."""

    def __init__(self, *args, on_change=None, **kwargs):
        self._on_change = on_change
        super().__init__(*args, **kwargs)

    def keypress(self, size, key):
        key_result = super().keypress(size, key)
        if self._on_change:
            self._on_change(self, self.edit_text)
        return key_result


class ResponseBox(urwid.WidgetWrap):
    """A scrollable response box that displays timestamped messages."""

    def __init__(self, max_responses=100, height=10):
        self.max_responses = max_responses
        self.responses = []
        self.height = height
        self._loop = None

        # Create the list walker and listbox
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)

        # Wrap in a LineBox for visual separation
        self.box = urwid.LineBox(self.listbox, title="Response Log")

        # Set fixed height
        self.pile = urwid.BoxAdapter(self.box, height=self.height)

        super().__init__(self.pile)

    def set_loop(self, loop):
        """Set the urwid main loop for triggering redraws."""
        self._loop = loop

    def _detect_style(self, message):
        """Detect message style from content."""
        if not message:
            return None
        for marker in ("✅", "✓", "Success", "successfully", "Bought", "Sold"):
            if marker in message:
                return "log_success"
        for marker in ("❌", "✗", "Failed", "Error", "error"):
            if marker in message:
                return "log_error"
        for marker in ("⚠", "Skipped", "skipping", "Timed out", "⏱"):
            if marker in message:
                return "log_warning"
        for marker in ("Submitting", "Fetching", "Order added", "🎯", "📊"):
            if marker in message:
                return "log_info"
        return None

    def add_separator(self, label=""):
        """Add a visual separator line."""
        sep = f"── {label} ──" if label else "─" * 40
        text_widget = urwid.Text(("log_separator", sep))
        self.walker.append(text_widget)
        if len(self.walker) > self.max_responses:
            self.walker.pop(0)

    def add_response(self, message, style=None, force_redraw=False):
        """Add a new response message with timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        if not style:
            style = self._detect_style(message)

        # Build markup: dim timestamp + colored message
        if style:
            markup = [("log_timestamp", f"[{timestamp}] "), (style, message)]
        else:
            markup = [("log_timestamp", f"[{timestamp}] "), message]

        text_widget = urwid.Text(markup)

        # Add to walker
        self.walker.append(text_widget)
        self.responses.append(f"[{timestamp}] {message}")

        # Keep only the last max_responses
        if len(self.walker) > self.max_responses:
            self.walker.pop(0)
            self.responses.pop(0)

        # Auto-scroll to bottom
        if len(self.walker) > 0:
            self.listbox.set_focus(len(self.walker) - 1)

        # Force screen redraw for real-time response display
        if force_redraw and self._loop:
            self._loop.draw_screen()

    def clear(self):
        """Clear all responses."""
        self.walker.clear()
        self.responses.clear()
        self.add_response("Response log cleared.")

    def _copy_to_clipboard(self, text):
        """Copy text to system clipboard."""
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=2)
                return True
            except (FileNotFoundError, subprocess.SubprocessError):
                continue
        return False

    def copy_line(self):
        """Copy the currently focused line to clipboard."""
        if not self.walker:
            return False
        try:
            focus_widget, _ = self.listbox.get_focus()
            if focus_widget:
                text = focus_widget.get_text()[0]
                if self._copy_to_clipboard(text):
                    self.add_response("Copied line to clipboard.", style="log_info")
                    return True
        except (IndexError, AttributeError):
            pass
        return False

    def copy_all(self):
        """Copy entire log to clipboard."""
        if not self.responses:
            return False
        text = "\n".join(self.responses)
        if self._copy_to_clipboard(text):
            self.add_response("Copied full log to clipboard.", style="log_info")
            return True
        return False

    def enter_focus_mode(self):
        """Enter scrollable focus mode with mouse tracking disabled."""
        if self._loop:
            self._loop.screen.set_mouse_tracking(False)
        self._focus_mode = True
        self.box.set_title("Response Log [FOCUS - ↑↓:scroll  y:copy line  Y:copy all  Esc/L:exit]")
        if self._loop:
            self._loop.draw_screen()

    def exit_focus_mode(self):
        """Exit focus mode, restore mouse tracking."""
        if self._loop:
            self._loop.screen.set_mouse_tracking(True)
        self._focus_mode = False
        self.box.set_title("Response Log")
        # Re-scroll to bottom
        if self.walker:
            self.listbox.set_focus(len(self.walker) - 1)
        if self._loop:
            self._loop.draw_screen()

    @property
    def in_focus_mode(self):
        return getattr(self, "_focus_mode", False)

    def focus_keypress(self, key):
        """Handle keys while in focus mode. Returns True if handled."""
        if key in ("esc", "l", "L"):
            self.exit_focus_mode()
            return True
        if key in ("up", "k"):
            try:
                pos = self.listbox.focus_position
                if pos > 0:
                    self.listbox.set_focus(pos - 1)
                    if self._loop:
                        self._loop.draw_screen()
            except (IndexError, AttributeError):
                pass
            return True
        if key in ("down", "j"):
            try:
                pos = self.listbox.focus_position
                if pos < len(self.walker) - 1:
                    self.listbox.set_focus(pos + 1)
                    if self._loop:
                        self._loop.draw_screen()
            except (IndexError, AttributeError):
                pass
            return True
        if key == "y":
            self.copy_line()
            return True
        if key == "Y":
            self.copy_all()
            return True
        return False

    def set_height(self, height):
        """Dynamically adjust the height of the response box."""
        self.height = height
        self.pile = urwid.BoxAdapter(self.box, height=self.height)
        self._w = self.pile


class AsyncioEventLoop(urwid.AsyncioEventLoop):
    """Custom asyncio event loop for urwid."""

    def run(self):
        self._loop.run_forever()
