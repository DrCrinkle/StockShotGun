import urwid
import asyncio
import traceback
from brokers import (
    robinTrade,
    tradierTrade,
    tastyTrade,
    publicTrade,
    firstradeTrade,
    fennelTrade,
    schwabTrade,
    bbaeTrade,
    dspacTrade,
    webullTrade,
    tradierGetHoldings,
    bbaeGetHoldings,
    dspacGetHoldings,
    webullGetHoldings,
    publicGetHoldings,
    tastyGetHoldings,
    robinGetHoldings,
    schwabGetHoldings,
    fennelGetHoldings,
    firstradeGetHoldings,
)

BROKERS = [
    "Robinhood",
    "Tradier",
    "TastyTrade",
    "Public",
    "Firstrade",
    "Fennel",
    "Schwab",
    "BBAE",
    "DSPAC",
    "Webull",
]


class EditWithCallback(urwid.Edit):
    def __init__(self, *args, on_change=None, **kwargs):
        self._on_change = on_change
        super().__init__(*args, **kwargs)

    def keypress(self, size, key):
        key_result = super().keypress(size, key)
        if self._on_change:
            self._on_change(self, self.edit_text)
        return key_result


class AsyncioEventLoop(urwid.AsyncioEventLoop):
    def run(self):
        self._loop.run_forever()


class HoldingsView:
    def __init__(self, holdings_data, broker_name):
        self.holdings_data = holdings_data
        self.broker_name = broker_name
        self.account_ids = list(holdings_data.keys())
        self.current_index = 0

    def get_current_account(self):
        return self.account_ids[self.current_index]

    def next_account(self):
        self.current_index = (self.current_index + 1) % len(self.account_ids)

    def prev_account(self):
        self.current_index = (self.current_index - 1) % len(self.account_ids)

    def get_current_holdings_text(self):
        account_id = self.get_current_account()
        positions = self.holdings_data[account_id]

        text = f"{self.broker_name} Holdings - Account {account_id} ({self.current_index + 1}/{len(self.account_ids)}):\n\n"

        if not positions:
            text += "No positions\n"
        else:
            for position in positions:
                text += (
                    f"{position['symbol']}: {position['quantity']} shares\n"
                    f"  Cost Basis: ${position['cost_basis']}\n"
                    f"  Current Value: ${position['current_value']}\n\n"
                )
        return text


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
            response.set_text("No brokers selected!")
            return
        if not all([current_order["ticker"], current_order["quantity"]]):
            response.set_text("Please fill in all order details (Ticker, Quantity)!")
            return

        orders.append(current_order.copy())
        response.set_text(f"Order added. Total orders: {len(orders)}")
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
            response.set_text("No orders to submit!")
            return

        response.set_text(f"Submitting {len(orders)} orders...")
        
        trade_functions = {
            "Robinhood": robinTrade,
            "Tradier": tradierTrade,
            "TastyTrade": tastyTrade,
            "Public": publicTrade,
            "Firstrade": firstradeTrade,
            "Fennel": fennelTrade,
            "Schwab": schwabTrade,
            "BBAE": bbaeTrade,
            "DSPAC": dspacTrade,
            "Webull": webullTrade,
        }

        for idx, order in enumerate(orders, 1):
            print(f"Submitting order {idx}: {order}")
            for broker in order["selected_brokers"]:
                trade_function = trade_functions.get(broker)
                print(f"Submitting to {broker}")
                if trade_function:
                    try:
                        await trade_function(
                            order["action"],
                            order["quantity"],
                            order["ticker"],
                            order["price"],
                        )
                    except Exception as e:
                        print(f"Error submitting order to {broker}: {str(e)}")
                        traceback.print_exc()

        response.set_text("All orders processed. Check console for details.")
        orders.clear()
        update_order_summary()

    async def show_holdings_screen(button):
        selected_brokers = current_order["selected_brokers"]
        if not selected_brokers:
            response.set_text("Please select a broker first!")
            return
        
        broker = selected_brokers[0]  # Get the first selected broker
        holdings_functions = {
            "Robinhood": robinGetHoldings,
            "Tradier": tradierGetHoldings,
            "BBAE": bbaeGetHoldings,
            "DSPAC": dspacGetHoldings,
            "Webull": webullGetHoldings,
            "Public": publicGetHoldings,
            "TastyTrade": tastyGetHoldings,
            "Schwab": schwabGetHoldings,
            "Fennel": fennelGetHoldings,
            "Firstrade": firstradeGetHoldings,
        }

        if broker not in holdings_functions:
            response.set_text(f"Holdings view not supported for {broker}!")
            return

        try:
            response.set_text(f"Fetching {broker} holdings...")
            loop.draw_screen()

            ticker_filter = current_order.get("ticker")
            holdings = await holdings_functions[broker](ticker_filter)

            if not holdings:
                response.set_text(f"No holdings found or error accessing {broker} account")
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
            response.set_text(f"Error checking holdings: {str(e)}")
            loop.draw_screen()

    def create_holdings_screen(holdings_data, broker_name):
        holdings_view = HoldingsView(holdings_data, broker_name)
        text_widget = urwid.Text("")
        response.set_text("")

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
    response = urwid.Text("Add orders and submit them all at once!")
    order_summary = urwid.Text("")
    main = create_main_menu()

    frame = urwid.Frame(
        body=urwid.Padding(main, left=2, right=2),
        footer=urwid.Pile(
            [
                urwid.Padding(response, left=2, right=2),
                urwid.Divider(),
                urwid.Padding(order_summary, left=2, right=2),
            ]
        ),
    )

    # Create the main loop
    event_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(event_loop)

    loop = urwid.MainLoop(
        frame,
        palette=[("reversed", "standout", ""), ("editcp", "light gray", "dark blue")],
        event_loop=urwid.AsyncioEventLoop(loop=event_loop),
        unhandled_input=default_input_handler,
    )

    try:
        loop.run()
    finally:
        event_loop.close()

