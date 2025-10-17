"""Main TUI application with run_tui() function."""

import sys
import urwid
import asyncio
import traceback

from .config import BROKERS
from .widgets import EditWithCallback, ResponseBox
from .holdings_view import HoldingsView
from .broker_functions import BROKER_CONFIG
from .response_handler import ResponseWriter
from .input_handler import tui_input_handler, setup_tui_input_interception, restore_original_input


def run_tui():
    orders = []
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
                "View Holdings",
                on_press=lambda button: asyncio.create_task(
                    show_holdings_screen(button)
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
            response_box.add_response("Please fill in all order details (Ticker, Quantity)!")
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
        summary = (
            "Current order:\n"
            f"Action: {current_order['action']}\n"
            f"Ticker: {current_order['ticker']}\n"
            f"Quantity: {current_order['quantity']}\n"
            f"Price: {current_order['price']}\n"
            f"Brokers: {', '.join(current_order['selected_brokers'])}\n\n"
            f"Total orders: {len(orders)}"
        )
        order_summary.set_text(summary)

    async def submit_all_orders(button):
        if not orders:
            response_box.add_response("No orders to submit!")
            return

        response_box.add_response(f"Submitting {len(orders)} orders...")

        for idx, order in enumerate(orders, 1):
            response_box.add_response(f"Submitting order {idx}: {order}")
            for broker in order["selected_brokers"]:
                broker_config = BROKER_CONFIG.get(broker)
                trade_function = broker_config.get("trade") if broker_config else None
                response_box.add_response(f"Submitting to {broker}")
                if trade_function:
                    try:
                        await trade_function(
                            order["action"],
                            order["quantity"],
                            order["ticker"],
                            order["price"],
                        )
                        response_box.add_response(f"✓ Successfully submitted to {broker}")
                    except Exception as e:
                        error_msg = f"✗ Error submitting order to {broker}: {str(e)}"
                        response_box.add_response(error_msg)
                        traceback.print_exc()

        response_box.add_response("All orders processed!")
        orders.clear()
        update_order_summary()

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
                response_box.add_response(f"No holdings found or error accessing {broker} account")
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

        body.extend([
            urwid.AttrMap(
                urwid.Button("Clear All Orders", on_press=lambda btn: handle_key("c")),
                None,
                focus_map="reversed",
            ),
            urwid.Divider(),
            urwid.AttrMap(
                urwid.Button("Back to Main Menu", on_press=show_main_screen),
                None,
                focus_map="reversed",
            ),
        ])

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
        if key in ("q", "Q"):
            raise urwid.ExitMainLoop()
        return False

    def show_main_screen(button=None):
        reset_current_order()
        main_view = create_main_menu()
        frame.body = urwid.Padding(main_view, left=2, right=2)
        loop.unhandled_input = default_input_handler
        loop.draw_screen()

    # Initialize the UI
    response_box = ResponseBox(max_responses=50, height=8)
    instruction_text = urwid.Text("Add orders and submit them all at once!")
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

    # Create the main loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    loop = urwid.MainLoop(
        frame,
        palette=[("reversed", "standout", ""), ("editcp", "light gray", "dark blue")],
        event_loop=urwid.AsyncioEventLoop(loop=event_loop),
        unhandled_input=default_input_handler,
    )

    # Set up input handler for the TUI
    tui_input_handler.set_loop(loop)
    setup_tui_input_interception()

    try:
        loop.run()
    finally:
        # Restore original input function
        restore_original_input()
        
        # Restore original stdout/stderr
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        event_loop.close()
