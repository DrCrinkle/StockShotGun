"""Input handling and modal dialogs for the TUI."""

import urwid
from .config import MODAL_WIDTH, MODAL_HEIGHT

# Store original input function before any modifications
original_input = input


class TUIInputHandler:
    """Handles input prompts within the TUI by showing modal dialogs."""

    def __init__(self):
        self.loop = None
        self.input_result = None
        self.waiting_for_input = False

    def set_loop(self, loop):
        self.loop = loop

    def prompt_user(self, prompt_text):
        """Show modal input dialog and wait for user input (sync, works from async code).

        This is a CRITICAL function that solves the sync/async impedance mismatch:
        - Brokers call input() synchronously (from async tasks)
        - But urwid needs the event loop to keep running to process keypresses
        - Solution: Manually pump the event loop while blocking the caller

        DO NOT MODIFY THIS WITHOUT UNDERSTANDING THE IMPLICATIONS!
        """
        if not self.loop:
            # Fallback to regular input if TUI not available
            return original_input(prompt_text)

        self.input_result = None
        self.waiting_for_input = True

        # Show the input modal immediately
        self._show_input_modal(prompt_text)

        # CRITICAL: Manually pump the event loop while waiting!
        # - _run_once() processes pending events (keypresses, timers, etc.)
        # - This keeps urwid responsive while blocking the input() call
        # - DO NOT use time.sleep() or threading.Event.wait() - they freeze the loop!
        event_loop = self.loop.event_loop._loop  # Get the asyncio event loop

        while self.waiting_for_input:
            # Process ONE iteration of the event loop
            # This allows keypresses to be processed!
            event_loop._run_once()

            # Also redraw screen
            self.loop.draw_screen()

        return self.input_result or ""

    def _show_input_modal(self, prompt_text):
        """Create and display the input modal dialog."""
        # Create input widget
        self.input_edit = urwid.Edit("")

        # Create dialog content
        dialog_content = [
            urwid.Text(("header", "📝 Input Required"), align='center'),
            urwid.Divider(),
            urwid.Text(prompt_text.strip()),
            urwid.Divider(),
            urwid.AttrMap(self.input_edit, "editcp"),
            urwid.Divider(),
            urwid.Columns([
                ('weight', 1, urwid.Button("Submit (Enter)", on_press=lambda btn: self._submit_input())),
                ('weight', 1, urwid.Button("Cancel (Esc)", on_press=lambda btn: self._cancel_input())),
            ], dividechars=2)
        ]

        # Create modal dialog
        dialog = urwid.LineBox(
            urwid.Filler(
                urwid.Pile(dialog_content),
                valign='middle'
            ),
            title=" User Input Required "
        )

        # Create overlay
        self.overlay = urwid.Overlay(
            dialog,
            self.loop.widget,
            align='center',
            width=MODAL_WIDTH,
            valign='middle',
            height=MODAL_HEIGHT
        )

        # Store original widget and show overlay
        self.original_widget = self.loop.widget
        self.loop.widget = self.overlay

        # Override the unhandled input to handle our modal
        self.original_unhandled_input = self.loop.unhandled_input
        self.loop.unhandled_input = self._handle_modal_input

        self.loop.draw_screen()

    def _handle_modal_input(self, key):
        """Handle input events in the modal dialog."""
        if key == 'enter':
            self._submit_input()
            return True
        elif key == 'esc':
            self._cancel_input()
            return True
        else:
            # Let the input widget handle other keys
            size = self.loop.screen.get_cols_rows()
            self.input_edit.keypress(size, key)
            self.loop.draw_screen()
            return True

    def _submit_input(self):
        """Handle input submission."""
        self.input_result = self.input_edit.edit_text
        self._close_modal()

    def _cancel_input(self):
        """Handle input cancellation."""
        self.input_result = ""
        self._close_modal()

    def _close_modal(self):
        """Close the modal dialog and restore original widget."""
        if hasattr(self, 'original_widget'):
            self.loop.widget = self.original_widget
            self.loop.unhandled_input = self.original_unhandled_input
            self.waiting_for_input = False
            self.loop.draw_screen()


# Global input handler
tui_input_handler = TUIInputHandler()


class TUICompatibleInput:
    """Custom input function that works with the TUI."""

    def __call__(self, prompt=""):
        if tui_input_handler.loop:
            return tui_input_handler.prompt_user(prompt)
        else:
            return original_input(prompt)


# Store original input function and create TUI-compatible version
tui_compatible_input = TUICompatibleInput()


def setup_tui_input_interception():
    """Setup input interception for the TUI."""
    __builtins__['input'] = tui_compatible_input


def restore_original_input():
    """Restore the original input function."""
    __builtins__['input'] = original_input
