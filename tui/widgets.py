"""Custom urwid widgets for the TUI."""

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

    def __init__(self, max_responses=50, height=8):
        self.max_responses = max_responses
        self.responses = []
        self.height = height

        # Create the list walker and listbox
        self.walker = urwid.SimpleFocusListWalker([])
        self.listbox = urwid.ListBox(self.walker)

        # Wrap in a LineBox for visual separation
        self.box = urwid.LineBox(self.listbox, title="Response Log")

        # Set fixed height
        self.pile = urwid.BoxAdapter(self.box, height=self.height)

        super().__init__(self.pile)

    def add_response(self, message, style=None, force_redraw=False):
        """Add a new response message with timestamp."""
        timestamp = datetime.now().strftime("%H:%M:%S")

        # Create the message text with timestamp
        formatted_msg = f"[{timestamp}] {message}"

        # Apply style if provided
        text_widget = urwid.Text((style, formatted_msg)) if style else urwid.Text(formatted_msg)

        # Add to walker
        self.walker.append(text_widget)
        self.responses.append(formatted_msg)

        # Keep only the last max_responses
        if len(self.walker) > self.max_responses:
            self.walker.pop(0)
            self.responses.pop(0)

        # Auto-scroll to bottom
        if len(self.walker) > 0:
            self.listbox.set_focus(len(self.walker) - 1)

    def clear(self):
        """Clear all responses."""
        self.walker.clear()
        self.responses.clear()
        self.add_response("Response log cleared.")

    def set_height(self, height):
        """Dynamically adjust the height of the response box."""
        self.height = height
        self.pile = urwid.BoxAdapter(self.box, height=self.height)
        self._w = self.pile


class AsyncioEventLoop(urwid.AsyncioEventLoop):
    """Custom asyncio event loop for urwid."""

    def run(self):
        self._loop.run_forever()
