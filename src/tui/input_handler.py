"""Input handling and modal dialogs for the TUI."""

import asyncio
from collections import deque
import urwid
from tui.config import MODAL_WIDTH, MODAL_HEIGHT
from cli_runtime import CliRuntimeError, ExitCode

# Store original input function before any modifications
original_input = input
non_interactive_mode = False


def set_non_interactive_mode(enabled=False):
    """Enable/disable fail-fast input behavior for non-interactive CLI runs."""
    global non_interactive_mode
    non_interactive_mode = enabled


class TUIInputHandler:
    """Handles input prompts within the TUI by showing modal dialogs."""

    def __init__(self):
        self.loop = None
        self._pending_requests = deque()
        self._active_request = None
        self._modal_widget = None
        self.input_edit = None

    def set_loop(self, loop):
        self.loop = loop

    async def async_prompt(self, prompt_text):
        """Show modal input dialog and await user input (async, no event loop pumping).

        This creates a Future, shows the modal, and awaits the Future.
        The event loop continues running normally - no _run_once() re-entrancy.
        When the user submits or cancels, the modal callback resolves the Future.
        """
        if not self.loop:
            return await asyncio.to_thread(original_input, prompt_text)

        future = asyncio.get_running_loop().create_future()
        self._pending_requests.append((prompt_text, future))
        if self._active_request is None:
            self._show_next_prompt()
        return await future

    def _show_next_prompt(self):
        """Create and display the input modal dialog."""
        loop = self.loop
        if loop is None or self._active_request is not None or not self._pending_requests:
            return

        prompt_text, future = self._pending_requests.popleft()
        self._active_request = (prompt_text, future)

        # Create input widget
        self.input_edit = urwid.Edit("")

        # Create dialog content
        dialog_content = [
            urwid.Text(("header", "Input Required"), align="center"),
            urwid.Divider(),
            urwid.Text(prompt_text.strip()),
            urwid.Divider(),
            urwid.AttrMap(self.input_edit, "editcp"),
            urwid.Divider(),
            urwid.Columns(
                [
                    (
                        1,
                        urwid.Button(
                            "Submit (Enter)", on_press=lambda btn: self._submit_input()
                        ),
                    ),
                    (
                        1,
                        urwid.Button(
                            "Cancel (Esc)", on_press=lambda btn: self._cancel_input()
                        ),
                    ),
                ],
                dividechars=2,
            ),
        ]

        # Create modal dialog
        dialog = urwid.LineBox(
            urwid.Filler(urwid.Pile(dialog_content), valign="middle"),
            title=" User Input Required ",
        )

        # Create overlay
        self._modal_widget = urwid.Overlay(
            dialog,
            loop.widget,
            align="center",
            width=MODAL_WIDTH,
            valign="middle",
            height=MODAL_HEIGHT,
        )

        # Store original widget and show overlay
        self.original_widget = loop.widget
        loop.widget = self._modal_widget

        # Override the unhandled input to handle our modal
        self.original_unhandled_input = loop.unhandled_input
        loop.unhandled_input = self._handle_modal_input

        loop.draw_screen()

    def _handle_modal_input(self, key):
        """Handle input events in the modal dialog."""
        loop = self.loop
        if loop is None:
            return False

        if key == "enter":
            self._submit_input()
            return True
        elif key == "esc":
            self._cancel_input()
            return True
        else:
            # Let the input widget handle other keys
            size = loop.screen.get_cols_rows()
            self.input_edit.keypress(size, key)
            loop.draw_screen()
            return True

    def _submit_input(self):
        """Handle input submission - resolves the pending future."""
        result = self.input_edit.edit_text
        self._finish_active_request(result)

    def _cancel_input(self):
        """Handle input cancellation - resolves with empty string."""
        self._finish_active_request("")

    def _finish_active_request(self, result):
        active_request = self._active_request
        if active_request is None:
            return

        _, future = active_request
        self._close_modal()
        self._active_request = None
        if not future.done():
            future.set_result(result)
        if self._pending_requests:
            self._show_next_prompt()

    def _close_modal(self):
        """Close the modal dialog and restore original widget."""
        loop = self.loop
        if loop and hasattr(self, "original_widget"):
            loop.widget = self.original_widget
            loop.unhandled_input = self.original_unhandled_input
            loop.draw_screen()
        self._modal_widget = None
        self.input_edit = None


# Global input handler
tui_input_handler = TUIInputHandler()


async def tui_async_input(prompt=""):
    """Async input() replacement for use in broker code running in the TUI.

    Use this instead of input() in async broker functions that may need
    user input (2FA codes, etc.) while the TUI is running.
    """
    if non_interactive_mode:
        raise CliRuntimeError(
            "Interactive input required but --non-interactive mode is enabled",
            ExitCode.NON_INTERACTIVE_INPUT_REQUIRED,
            details={"prompt": prompt},
        )

    if tui_input_handler.loop:
        return await tui_input_handler.async_prompt(prompt)
    else:
        return await asyncio.to_thread(original_input, prompt)


class TUICompatibleInput:
    """Custom input function that works with the TUI (sync fallback).

    For async broker code, use tui_async_input() instead.
    """

    def __call__(self, prompt=""):
        if non_interactive_mode:
            raise CliRuntimeError(
                "Interactive input required but --non-interactive mode is enabled",
                ExitCode.NON_INTERACTIVE_INPUT_REQUIRED,
                details={"prompt": prompt},
            )

        # In TUI mode, sync input() can't work with Python 3.14's strict
        # re-entrancy checks. Fall back to original input (prints to redirected stdout).
        return original_input(prompt)


# Store original input function and create TUI-compatible version
tui_compatible_input = TUICompatibleInput()


def setup_tui_input_interception():
    """Setup input interception for the TUI."""
    import builtins

    builtins.input = tui_compatible_input


def restore_original_input():
    """Restore the original input function."""
    import builtins

    builtins.input = original_input
