"""Main TUI application with run_tui() function."""

import sys
import urwid
import asyncio
import traceback
from collections.abc import Hashable
from typing import Any, Callable

from tui.config import BROKERS
from tui.widgets import EditWithCallback, ResponseBox
from tui.holdings_view import HoldingsView
from brokers.registry import broker_functions_map
from tui.response_handler import ResponseWriter

# Per-broker functions derived from the broker registry (ADR 0004).
BROKER_CONFIG = broker_functions_map()
from tui.input_handler import (
    tui_input_handler,
    setup_tui_input_interception,
    restore_original_input,
)
from cli.common import (
    _leg_label,
    aggregate_execution_results,
    get_engine,
    render_execution_result,
)
from enforcement import GateError
from robin_stocks.robinhood.helper import (
    set_output as set_robinhood_output,
    get_output as get_robinhood_output,
)


async def submit_orders_via_engine(
    orders: list[dict[str, Any]],
    *,
    engine: Any,
    progress_fn: Callable[..., None],
) -> dict[str, Any]:
    """Propose-then-execute a batch of TUI orders on the ExecutionEngine
    (ADR 0006), returning the legacy `{successful, failed, skipped,
    statuses}` shape the TUI's response panel and broker-status widgets
    already know how to read.

    Two phases, mirroring `apply_main_py_gate_batch` -> `execute_via_router`
    (the retired `agentic.cli_bridge` path this replaces):

      Phase 1 — propose EVERY order first. `GateError` raised here (all
      orders rejected before any of them execute) propagates to the caller
      unchanged — the TUI's `except GateError` handling around this call is
      what displays the rejection and clears the queue, exactly as it did
      for `apply_main_py_gate_batch`.

      Phase 2 — execute every proposal in order. This is interactive UI code
      and must NEVER raise on a rejected execution (unlike the CLI/batch/
      automate paths, which abort the whole process on an execute-time
      rejection). A `rejected=True` execution instead renders as an
      all-failed leg set for that order's selected brokers — the same
      outcome `execute_via_router` produced when `result.get("rejected")`
      was true (it marked every selected broker as failed and kept going).
      `render_execution_result` doesn't know the order's originally
      selected brokers (a rejection carries no broker/account list), so the
      synthetic all-failed status is built here rather than in the shared
      renderer.
    """
    def _progress(*args: Any, **kwargs: Any) -> None:
        """Best-effort UI update. The progress callback is purely cosmetic
        (it writes to the TUI response panel); a throwing callback must NEVER
        abort order execution or corrupt the aggregate result, so every
        invocation is swallowed — mirroring the retired bridge's try/except
        around each `progress_fn` call.
        """
        try:
            progress_fn(*args, **kwargs)
        except Exception:
            pass

    proposals: list[dict[str, Any]] = []
    for order in orders:
        proposal = await engine.propose_order(
            ticker=str(order["ticker"]),
            qty=order["quantity"],
            side=str(order["action"]),
            brokers=list(order["selected_brokers"]),
            price=order.get("price"),
            dry_run=False,
        )
        proposals.append(proposal)

    rendered_results: list[dict[str, Any]] = []
    for order, proposal in zip(orders, proposals):
        ticker = str(order["ticker"])
        action = str(order["action"])
        qty = order["quantity"]
        order_brokers = [str(b) for b in order.get("selected_brokers", [])]

        _progress(
            f"[tui] executing proposal {proposal['proposal_id'][:12]}… for "
            f"{action} {qty} {ticker} ({proposal['leg_count']} leg(s))"
        )

        execution = await engine.execute_order(
            proposal_id=proposal["proposal_id"],
            dry_run=False,
        )

        if execution.get("rejected"):
            _progress(
                f"✗ Order rejected by enforcement gate "
                f"({execution.get('reason')}): {execution.get('detail')}"
            )
            rendered = {
                "successful": 0,
                "failed": len(order_brokers),
                "skipped": 0,
                "statuses": [
                    {
                        "ticker": ticker,
                        "action": action,
                        "successful": [],
                        "failed": order_brokers,
                        "skipped": [],
                        "reason": execution.get("reason"),
                        "detail": execution.get("detail"),
                    }
                ],
            }
        else:
            rendered = render_execution_result(execution)
            # Emit per-leg progress from the raw execution legs (not the
            # rendered status, which collapses legs to bare broker labels and
            # discards each leg's reason/detail). This restores the retired
            # bridge's `✗ {broker}: {reason} - {detail}` diagnostics, with a
            # graceful fallback to `✗ {broker}: failed` when a failed leg
            # carries neither reason nor detail (no dangling `- ` or `None`).
            for leg in execution.get("results") or []:
                label = _leg_label(leg.get("broker", ""), leg.get("account_id"))
                if leg.get("ok"):
                    _progress(f"✓ {label}: {leg.get('detail') or 'placed'}")
                else:
                    reason = leg.get("reason")
                    detail = leg.get("detail")
                    if reason and detail:
                        _progress(f"✗ {label}: {reason} - {detail}")
                    elif reason:
                        _progress(f"✗ {label}: {reason}")
                    elif detail:
                        _progress(f"✗ {label}: {detail}")
                    else:
                        _progress(f"✗ {label}: failed")

        rendered_results.append(rendered)

    return aggregate_execution_results(rendered_results)


def run_tui():
    orders = []
    broker_statuses = {}
    auth_spinner_index = 0
    last_submitted_orders = []
    current_order = {
        "action": "buy",
        "quantity": None,
        "ticker": None,
        "price": None,
        "selected_brokers": [],
    }

    def create_main_menu():
        body = [urwid.Text("Brokers:"), urwid.Divider()]

        def toggle_buy_sell(button):
            current_order["action"] = (
                "sell" if current_order["action"] == "buy" else "buy"
            )
            button.set_label(current_order["action"].capitalize())

        for c in BROKERS:
            checkbox = urwid.CheckBox(c)
            urwid.connect_signal(checkbox, "change", broker_toggle)
            body.append(urwid.AttrMap(checkbox, None, focus_map="reversed"))

        buy_sell_button = urwid.Button("Buy", on_press=toggle_buy_sell)
        body.append(urwid.AttrMap(buy_sell_button, None, focus_map="reversed"))

        body.append(
            EditWithCallback(
                ("editcp", "Ticker Symbol: "),
                "",
                align="left",
                wrap="clip",
                multiline=False,
                allow_tab=False,
                on_change=edit_ticker,
            )
        )
        body.append(
            EditWithCallback(
                ("editcp", "Quantity: "),
                "",
                align="left",
                wrap="clip",
                multiline=False,
                allow_tab=False,
                on_change=edit_quantity,
            )
        )
        body.append(
            EditWithCallback(
                ("editcp", "Limit Price (optional): "),
                "",
                align="left",
                wrap="clip",
                multiline=False,
                allow_tab=False,
                on_change=edit_price,
            )
        )
        body.append(urwid.Button("Add Order", on_press=add_order))
        body.append(
            urwid.Button(
                "Submit All Orders",
                on_press=lambda button: asyncio.create_task(submit_all_orders(button)),
            )
        )
        body.append(
            urwid.Button(
                "Retry Timed-Out Brokers",
                on_press=lambda button: asyncio.create_task(
                    retry_timed_out_brokers(button)
                ),
            )
        )
        body.append(
            urwid.Button(
                "View Holdings",
                on_press=lambda button: asyncio.create_task(
                    show_holdings_screen(button)
                ),
            )
        )
        body.append(
            urwid.Button(
                "Auth Browsers",
                on_press=lambda button: asyncio.create_task(
                    auth_browser_brokers(button)
                ),
            )
        )
        body.append(urwid.Button("View Queued Orders", on_press=show_queued_orders))
        body.append(urwid.Button("Exit", on_press=exit_program))

        return urwid.ListBox(urwid.SimpleFocusListWalker(body))

    def broker_toggle(checkbox, state):
        broker = checkbox.get_label()
        selected = current_order["selected_brokers"]
        if state:
            selected.append(broker) if broker not in selected else None
        else:
            selected.remove(broker) if broker in selected else None
        update_order_summary()

    def edit_ticker(edit, content):
        current_order["ticker"] = content.upper()
        update_order_summary()

    def edit_quantity(edit, content):
        try:
            current_order["quantity"] = int(content)
        except ValueError:
            current_order["quantity"] = None
        update_order_summary()

    def edit_price(edit, content):
        try:
            current_order["price"] = float(content)
        except ValueError:
            current_order["price"] = None
        update_order_summary()

    def exit_program(button):
        raise urwid.ExitMainLoop()

    def add_order(button):
        if not current_order["selected_brokers"]:
            response_box.add_response("No brokers selected!")
            return
        if not all([current_order["ticker"], current_order["quantity"]]):
            response_box.add_response(
                "Please fill in all order details (Ticker, Quantity)!"
            )
            return

        orders.append(current_order.copy())
        response_box.add_response(f"Order added. Total orders: {len(orders)}")
        reset_current_order()
        update_order_summary()

    def reset_current_order():
        current_order.update(
            {
                "action": "buy",
                "quantity": None,
                "ticker": None,
                "price": None,
                "selected_brokers": [],
            }
        )

        # Reset UI elements
        for widget in main.body:
            base = widget.base_widget
            if isinstance(base, urwid.CheckBox):
                base.set_state(False)
            elif isinstance(base, EditWithCallback):
                base.set_edit_text("")
            elif isinstance(base, urwid.Button) and base.label in ["Buy", "Sell"]:
                base.set_label("Buy")

    def update_order_summary():
        display_price = current_order["price"]
        if display_price is None:
            display_price = "market"
        summary = (
            "Current order:\n"
            f"Action: {current_order['action']}\n"
            f"Ticker: {current_order['ticker']}\n"
            f"Quantity: {current_order['quantity']}\n"
            f"Price: {display_price}\n"
            f"Brokers: {', '.join(current_order['selected_brokers'])}\n\n"
            f"Total orders: {len(orders)}"
        )
        order_summary.set_text(summary)

    def update_broker_status_text():
        nonlocal auth_spinner_index
        if not broker_statuses:
            broker_status_text.set_text("Broker Status: idle")
            return

        by_status = {
            "ready": [],
            "authing": [],
            "timed-out": [],
            "failed": [],
            "skipped": [],
            "queued": [],
        }

        for broker, status in broker_statuses.items():
            by_status.setdefault(status, [])
            by_status[status].append(broker)

        status_styles = {
            "ready": "status_ready",
            "authing": "status_authing",
            "timed-out": "status_timeout",
            "failed": "status_failed",
            "skipped": "status_skipped",
            "queued": "status_queued",
        }

        parts = []
        ordered_statuses = [
            "ready",
            "authing",
            "timed-out",
            "failed",
            "skipped",
            "queued",
        ]
        for status in ordered_statuses:
            if by_status.get(status):
                brokers = ", ".join(sorted(by_status[status]))
                style = status_styles.get(status, "status_label")
                if status == "authing":
                    spinner_frames = ["-", "\\", "|", "/"]
                    spinner = spinner_frames[auth_spinner_index % len(spinner_frames)]
                    parts.append((style, f"{status}{spinner}: {brokers}"))
                else:
                    parts.append((style, f"{status}: {brokers}"))

        if parts:
            ready_count = len(by_status["ready"])
            authing_count = len(by_status["authing"])
            timed_out_count = len(by_status["timed-out"])
            failed_count = len(by_status["failed"])
            skipped_count = len(by_status["skipped"])
            queued_count = len(by_status["queued"])
            markup: list[str | tuple[Hashable, str]] = [
                (
                    "status_label",
                    (
                        "Broker Status "
                        f"(R:{ready_count} A:{authing_count} T:{timed_out_count} "
                        f"F:{failed_count} S:{skipped_count} Q:{queued_count}) | "
                    ),
                )
            ]
            for index, part in enumerate(parts):
                markup.append(part)
                if index < len(parts) - 1:
                    markup.append(("status_label", " | "))
            broker_status_text.set_text(markup)
        else:
            broker_status_text.set_text("Broker Status: idle")

    def on_broker_status_update(broker, status):
        broker_statuses[broker] = status
        update_broker_status_text()
        loop.draw_screen()

    async def submit_all_orders(button):
        nonlocal last_submitted_orders
        if not orders:
            response_box.add_response("No orders to submit!")
            return

        response_box.add_separator("Submitting Orders")
        response_box.add_response(f"Submitting {len(orders)} orders...")
        broker_statuses.clear()
        for order in orders:
            for broker in order.get("selected_brokers", []):
                broker_statuses[broker] = "queued"
        update_broker_status_text()

        # Broker validate fns for pre-flight (trade fns are dispatched by the Router).
        validate_functions = {
            broker: config["validate"]
            for broker, config in BROKER_CONFIG.items()
            if "validate" in config
        }
        last_submitted_orders = [
            {
                **order,
                "selected_brokers": list(order.get("selected_brokers", [])),
            }
            for order in orders
        ]

        try:
            # Pre-flight each order before proposing; drop legs that fail
            # broker validation and any order left with no executable broker.
            engine = await get_engine()
            executable_orders = []
            for order in orders:
                validated, skipped = await engine.validate_targets(
                    selected_brokers=list(order.get("selected_brokers", [])),
                    action=order["action"],
                    quantity=order["quantity"],
                    ticker=order["ticker"],
                    price=order.get("price"),
                    validate_functions=validate_functions,
                )
                for broker, reason in skipped:
                    broker_statuses[broker] = "skipped"
                    response_box.add_response(f"   ⚠ {broker}: Skipped ({reason})")
                if validated:
                    order["selected_brokers"] = validated
                    executable_orders.append(order)
            update_broker_status_text()
            if not executable_orders:
                response_box.add_response(
                    "All brokers failed pre-flight validation; nothing to execute",
                    force_redraw=True,
                )
                orders.clear()
                update_order_summary()
                return
            orders[:] = executable_orders

            # ADR 0006 — one propose path for every caller. `propose_order`
            # raises GateError on the FIRST rejection, aborting the whole
            # batch before anything executes — same fail-fast contract
            # `apply_main_py_gate_batch` gave this call site.
            try:
                results = await submit_orders_via_engine(
                    orders,
                    engine=engine,
                    progress_fn=lambda msg, force_redraw=False: response_box.add_response(
                        msg, force_redraw=force_redraw
                    ),
                )
            except GateError as gate_err:
                response_box.add_response(
                    f"✗ Order rejected by enforcement gate "
                    f"({gate_err.reason}): {gate_err}",
                    force_redraw=True,
                )
                orders.clear()
                update_order_summary()
                return

            # Build summary with total broker counts across all orders
            summary_parts = [f"✅ {results['successful']} succeeded"]
            if results["failed"] > 0:
                summary_parts.append(f"❌ {results['failed']} failed")
            if results["skipped"] > 0:
                summary_parts.append(f"⚠️ {results['skipped']} skipped")

            response_box.add_separator()
            response_box.add_response(f"🎯 Total Results: {', '.join(summary_parts)}", force_redraw=True)
        except Exception as e:
            response_box.add_response(f"✗ Error processing orders: {str(e)}")
            traceback.print_exc()

        orders.clear()
        update_order_summary()

    def get_timed_out_brokers():
        return sorted(
            [
                broker
                for broker, status in broker_statuses.items()
                if status == "timed-out"
            ]
        )

    async def retry_timed_out_brokers(button):
        timed_out_brokers = set(get_timed_out_brokers())
        if not timed_out_brokers:
            response_box.add_response("No timed-out brokers to retry.")
            return

        if not last_submitted_orders:
            response_box.add_response("No previous submission found to retry.")
            return

        retry_orders = []
        for order in last_submitted_orders:
            selected = [
                broker
                for broker in order.get("selected_brokers", [])
                if broker in timed_out_brokers
            ]
            if selected:
                retry_orders.append(
                    {
                        **order,
                        "selected_brokers": selected,
                    }
                )

        if not retry_orders:
            response_box.add_response(
                "No matching timed-out brokers in last submission."
            )
            return

        response_box.add_separator("Retrying Timed-Out Brokers")
        response_box.add_response(
            f"Retrying: {', '.join(sorted(timed_out_brokers))}"
        )

        for broker in timed_out_brokers:
            broker_statuses[broker] = "queued"
        update_broker_status_text()

        validate_functions = {
            broker: config["validate"]
            for broker, config in BROKER_CONFIG.items()
            if "validate" in config
        }

        try:
            # Pre-flight the retried orders before proposing, same as submit.
            engine = await get_engine()
            executable_retry = []
            for order in retry_orders:
                validated, skipped = await engine.validate_targets(
                    selected_brokers=list(order.get("selected_brokers", [])),
                    action=order["action"],
                    quantity=order["quantity"],
                    ticker=order["ticker"],
                    price=order.get("price"),
                    validate_functions=validate_functions,
                )
                for broker, reason in skipped:
                    broker_statuses[broker] = "skipped"
                    response_box.add_response(f"   ⚠ {broker}: Skipped ({reason})")
                if validated:
                    order["selected_brokers"] = validated
                    executable_retry.append(order)
            update_broker_status_text()
            if not executable_retry:
                response_box.add_response(
                    "All retried brokers failed pre-flight validation.",
                    force_redraw=True,
                )
                return
            retry_orders[:] = executable_retry

            # ADR 0006 — propose every retried order before executing any of
            # them; a GateError here aborts the whole retry, same contract
            # `apply_main_py_gate_batch` gave this call site.
            try:
                results = await submit_orders_via_engine(
                    retry_orders,
                    engine=engine,
                    progress_fn=lambda msg, force_redraw=False: response_box.add_response(
                        msg, force_redraw=force_redraw
                    ),
                )
            except GateError as gate_err:
                response_box.add_response(
                    f"✗ Retry rejected by enforcement gate "
                    f"({gate_err.reason}): {gate_err}",
                    force_redraw=True,
                )
                return

            response_box.add_response(
                (
                    f"Retry Results: ✅ {results['successful']} succeeded"
                    f", ❌ {results['failed']} failed"
                    f", ⚠️ {results['skipped']} skipped"
                )
            )
        except Exception as e:
            response_box.add_response(f"✗ Error retrying brokers: {str(e)}")
            traceback.print_exc()

    async def show_holdings_screen(button):
        selected_brokers = current_order["selected_brokers"]
        if not selected_brokers:
            response_box.add_response("Please select a broker first!")
            return

        broker = selected_brokers[0]  # Get the first selected broker
        broker_config = BROKER_CONFIG.get(broker)

        if not broker_config or "holdings" not in broker_config:
            response_box.add_response(f"Holdings view not supported for {broker}!")
            return

        try:
            response_box.add_response(f"Fetching {broker} holdings...")
            loop.draw_screen()

            ticker_filter = current_order.get("ticker")
            holdings_function = broker_config["holdings"]
            holdings = await holdings_function(ticker_filter)

            if not holdings:
                response_box.add_response(
                    f"No holdings found or error accessing {broker} account"
                )
                return

            holdings_view, key_handler = create_holdings_screen(holdings, broker)
            frame.body = holdings_view

            original_unhandled_input = loop.unhandled_input

            def handle_input(key):
                if not key_handler(key) and original_unhandled_input:
                    original_unhandled_input(key)

            loop.unhandled_input = handle_input
            loop.draw_screen()

        except Exception as e:
            response_box.add_response(f"Error checking holdings: {str(e)}")
            loop.draw_screen()

    async def auth_browser_brokers(button):
        """Pre-authenticate selected browser-based brokers."""
        from brokers.session_manager import session_manager

        all_browser_brokers = ["Chase", "WellsFargo", "SoFi", "Webull"]
        selected = current_order["selected_brokers"]
        browser_brokers = [b for b in all_browser_brokers if b in selected]

        if not browser_brokers:
            response_box.add_response(
                "No browser brokers selected. Select Chase, WellsFargo, SoFi, or Webull first.",
                force_redraw=True,
            )
            return

        authed = []
        failed = []

        for broker_name in browser_brokers:
            session = await session_manager.get_session(broker_name)
            if not session or not isinstance(session, dict):
                continue

            # Webull uses token-based auth (api_login / Zendriver capture)
            if broker_name == "Webull":
                profiles = session.get("profiles") or []
                auth_ok = False
                if profiles:
                    # Test if existing session has a valid trade token
                    wb_client = profiles[0].get("client")
                    if wb_client:
                        trade_token = str(getattr(wb_client, "_trade_token", "") or "").strip()
                        if trade_token:
                            auth_ok = True
                if auth_ok:
                    authed.append(broker_name)
                else:
                    response_box.add_response(f"Authenticating {broker_name} via browser...")
                    loop.draw_screen()
                    try:
                        from brokers.webull import reauth_webull_session

                        new_session = await reauth_webull_session(session_manager)
                        if new_session:
                            authed.append(broker_name)
                        else:
                            failed.append(broker_name)
                    except Exception as e:
                        response_box.add_response(f"{broker_name} auth failed: {e}")
                        failed.append(broker_name)
                continue

            client = session.get("client")
            if not client:
                # SoFi uses cookie-based auth, trigger it now
                if broker_name == "SoFi":
                    response_box.add_response(f"Authenticating {broker_name}...")
                    loop.draw_screen()
                    try:
                        from brokers.sofi import _sofi_get_cookies

                        cookies = await _sofi_get_cookies(session)
                        if cookies:
                            authed.append(broker_name)
                        else:
                            failed.append(broker_name)
                    except Exception as e:
                        response_box.add_response(f"{broker_name} auth failed: {e}")
                        failed.append(broker_name)
                continue

            if hasattr(client, "_is_authenticated") and client._is_authenticated:
                authed.append(broker_name)
                continue

            response_box.add_response(f"Authenticating {broker_name}...")
            loop.draw_screen()
            try:
                await client._ensure_authenticated()
                authed.append(broker_name)
            except Exception as e:
                response_box.add_response(f"{broker_name} auth failed: {e}")
                failed.append(broker_name)

        if authed:
            response_box.add_response(
                f"Browser auth complete: {', '.join(authed)}", force_redraw=True
            )
        if failed:
            response_box.add_response(
                f"Browser auth failed: {', '.join(failed)}", force_redraw=True
            )
        if not authed and not failed:
            response_box.add_response(
                "No browser brokers configured.", force_redraw=True
            )

    def show_queued_orders(button):
        """Display all queued orders."""
        if not orders:
            response_box.add_response("No orders queued!")
            return

        queued_orders_view, key_handler = create_queued_orders_screen()
        frame.body = queued_orders_view

        original_unhandled_input = loop.unhandled_input

        def handle_input(key):
            if not key_handler(key) and original_unhandled_input:
                original_unhandled_input(key)

        loop.unhandled_input = handle_input
        loop.draw_screen()

    def create_queued_orders_screen():
        """Create the queued orders view screen."""
        body = [
            urwid.Text(("reversed", f"Queued Orders ({len(orders)} total)")),
            urwid.Divider(),
        ]

        for idx, order in enumerate(orders, 1):
            order_type = order["action"].upper()
            ticker = order["ticker"]
            quantity = order["quantity"]
            price = f"${order['price']}" if order["price"] else "Market"
            brokers = ", ".join(order["selected_brokers"])

            order_text = [
                urwid.Text(("reversed", f"Order #{idx}")),
                urwid.Text(f"  Action: {order_type}"),
                urwid.Text(f"  Ticker: {ticker}"),
                urwid.Text(f"  Quantity: {quantity}"),
                urwid.Text(f"  Price: {price}"),
                urwid.Text(f"  Brokers: {brokers}"),
                urwid.Divider(),
            ]
            body.extend(order_text)

        def handle_key(key):
            match key:
                case "esc" | "q" | "b":
                    show_main_screen()
                    return True
                case "c":  # Clear all orders
                    if orders:
                        orders.clear()
                        update_order_summary()
                        show_main_screen()
                        response_box.add_response("All orders cleared!")
                    return True
                case _:
                    return False

        body.extend(
            [
                urwid.AttrMap(
                    urwid.Button(
                        "Clear All Orders", on_press=lambda btn: handle_key("c")
                    ),
                    None,
                    focus_map="reversed",
                ),
                urwid.Divider(),
                urwid.AttrMap(
                    urwid.Button("Back to Main Menu", on_press=show_main_screen),
                    None,
                    focus_map="reversed",
                ),
            ]
        )

        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(body))
        return (
            urwid.Frame(
                listbox,
                footer=urwid.Text("ESC/Q/B: Back | C: Clear All Orders"),
                focus_part="body",
            ),
            handle_key,
        )

    def create_holdings_screen(holdings_data, broker_name):
        holdings_view = HoldingsView(holdings_data, broker_name)
        text_widget = urwid.Text("")

        def update_display():
            text_widget.set_text(holdings_view.get_current_holdings_text())

        def handle_key(key):
            match key:
                case "left" | "h":
                    holdings_view.prev_account()
                    update_display()
                    return True
                case "right" | "l":
                    holdings_view.next_account()
                    update_display()
                    return True
                case "esc" | "q" | "b":
                    show_main_screen()
                    return True
                case _:
                    return False

        body = [
            urwid.Text("← Use Left/Right Arrow Keys to Navigate Accounts →"),
            urwid.Divider(),
            text_widget,
            urwid.Divider(),
            urwid.AttrMap(
                urwid.Button("Back to Main Menu", on_press=show_main_screen),
                None,
                focus_map="reversed",
            ),
        ]

        update_display()

        listbox = urwid.ListBox(urwid.SimpleFocusListWalker(body))
        return (
            urwid.Frame(
                listbox,
                footer=urwid.Text("ESC/Q/B: Back to Main Menu"),
                focus_part="body",
            ),
            handle_key,
        )

    def default_input_handler(key):
        if response_box.in_focus_mode:
            return response_box.focus_keypress(key)
        if key in ("q", "Q"):
            raise urwid.ExitMainLoop()
        if key in ("v", "V"):
            level = response_writer.cycle_verbosity()
            response_box.add_response(f"Log verbosity set to: {level}")
            return True
        if key in ("l", "L"):
            response_box.enter_focus_mode()
            return True
        return False

    def show_main_screen(button=None):
        reset_current_order()
        main_view = create_main_menu()
        frame.body = urwid.Padding(main_view, left=2, right=2)
        loop.unhandled_input = default_input_handler
        loop.draw_screen()

    # Initialize the UI
    response_box = ResponseBox(max_responses=100, height=15)
    instruction_text = urwid.Text(
        "Add orders and submit them all at once! (V: log verbosity, L: focus log)"
    )
    broker_status_text = urwid.Text("Broker Status: idle")
    order_summary = urwid.Text("")
    main = create_main_menu()

    frame = urwid.Frame(
        body=urwid.Padding(main, left=2, right=2),
        footer=urwid.Pile(
            [
                response_box,
                urwid.Divider(),
                urwid.Padding(instruction_text, left=2, right=2),
                urwid.Divider(),
                urwid.Padding(broker_status_text, left=2, right=2),
                urwid.Divider(),
                urwid.Padding(order_summary, left=2, right=2),
            ]
        ),
    )

    # Redirect stdout/stderr to response box
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    response_writer = ResponseWriter(response_box.add_response)
    sys.stdout = response_writer
    sys.stderr = response_writer
    original_rh_output = get_robinhood_output()
    set_robinhood_output(response_writer)

    # Create the main loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    loop = urwid.MainLoop(
        frame,
        palette=[
            ("reversed", "standout", ""),
            ("editcp", "light gray", "dark blue"),
            ("status_label", "light gray", ""),
            ("status_ready", "light green", ""),
            ("status_authing", "yellow", ""),
            ("status_timeout", "light red", ""),
            ("status_failed", "light red", ""),
            ("status_skipped", "light cyan", ""),
            ("status_queued", "light blue", ""),
            ("log_timestamp", "dark gray", ""),
            ("log_success", "light green", ""),
            ("log_error", "light red", ""),
            ("log_warning", "yellow", ""),
            ("log_info", "light cyan", ""),
            ("log_separator", "dark gray", ""),
        ],
        event_loop=urwid.AsyncioEventLoop(loop=event_loop),
        unhandled_input=default_input_handler,
    )

    async def auth_spinner_loop():
        nonlocal auth_spinner_index
        while True:
            await asyncio.sleep(0.25)
            if any(status == "authing" for status in broker_statuses.values()):
                auth_spinner_index = (auth_spinner_index + 1) % 4
                update_broker_status_text()
                loop.draw_screen()

    spinner_task = event_loop.create_task(auth_spinner_loop())

    # Set loop on response_box for real-time response display
    response_box.set_loop(loop)

    # Set up input handler for the TUI
    tui_input_handler.set_loop(loop)
    setup_tui_input_interception()

    # Override urwid's exception handler AFTER loop.run() installs it.
    # urwid's default _exception_handler calls loop.stop() on ANY exception from
    # ANY asyncio callback, which kills the TUI if a single broker task raises.
    # For a multi-broker app with concurrent browser automation, we need to be
    # more resilient: only stop on ExitMainLoop (intentional quit).
    urwid_event_loop = loop.event_loop

    def _resilient_exception_handler(aloop, context):
        exc = context.get("exception")
        if isinstance(exc, urwid.ExitMainLoop):
            aloop.stop()
            if urwid_event_loop._idle_asyncio_handle:
                urwid_event_loop._idle_asyncio_handle.cancel()
                urwid_event_loop._idle_asyncio_handle = None
        elif exc:
            # Log but don't stop - broker errors shouldn't crash the TUI
            response_box.add_response(f"Background error: {exc}")
        else:
            aloop.default_exception_handler(context)

    # Monkey-patch urwid's run to install our handler after urwid sets its own
    def _run_with_handler():
        event_loop.set_exception_handler(_resilient_exception_handler)
        event_loop.run_forever()
        if urwid_event_loop._exc:
            exc = urwid_event_loop._exc
            urwid_event_loop._exc = None
            raise exc.with_traceback(exc.__traceback__)

    urwid_event_loop.run = _run_with_handler

    try:
        loop.run()
    finally:
        spinner_task.cancel()
        try:
            event_loop.run_until_complete(spinner_task)
        except asyncio.CancelledError:
            pass
        # Restore original input function
        restore_original_input()

        # Restore original stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        set_robinhood_output(original_rh_output)
        event_loop.close()
