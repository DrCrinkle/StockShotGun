import argparse
import asyncio
from brokers import robinTrade, tradierTrade, tastyTrade, publicTrade, firstradeTrade, fennelTrade, schwabTrade, bbaeTrade, dspacTrade
from setup import setup
import urwid
import traceback

selected_brokers = []

class EditWithCallback(urwid.Edit):
    def __init__(self, *args, on_change=None, **kwargs):
        self._on_change = on_change
        super().__init__(*args, **kwargs)

    def keypress(self, size, key):
        # Handle the keypress as usual
        key_result = super().keypress(size, key)
        if self._on_change:
            self._on_change(self, self.edit_text)
        return key_result

def run_tui():
    brokers = [
        "Robinhood", "Tradier", "TastyTrade", "Public", "Firstrade", "Fennel", "Schwab", "BBAE", "DSPAC", "Chase"
    ]
    order_details = {
        "action": None,
        "quantity": None,
        "ticker": None,
        "price": None
    }

    def menu(title, choices):
        body = [urwid.Text(title), urwid.Divider()]

        def toggle_buy_sell(button):
                current_action = order_details.get('action', 'buy')
                new_action = 'sell' if current_action == 'buy' else 'buy'
                order_details['action'] = new_action
                button.set_label(new_action.capitalize())
        
        for c in choices:
            checkbox = urwid.CheckBox(c)
            urwid.connect_signal(checkbox, 'change', broker_toggle)
            body.append(urwid.AttrMap(checkbox, None, focus_map='reversed'))
        
        # Toggle for Buy/Sell
        buy_sell_button = urwid.Button('Buy', on_press=toggle_buy_sell)
        body.append(urwid.AttrMap(buy_sell_button, None, focus_map='reversed'))
        
        body.append(EditWithCallback(('editcp', 'Ticker Symbol: '), '', align='left', wrap='clip', multiline=False, allow_tab=False, on_change=edit_ticker))
        body.append(EditWithCallback(('editcp', 'Quantity: '), '', align='left', wrap='clip', multiline=False, allow_tab=False, on_change=edit_quantity))
        body.append(EditWithCallback(('editcp', 'Limit Price (optional): '), '', align='left', wrap='clip', multiline=False, allow_tab=False, on_change=edit_price))
        body.append(urwid.Button("Submit Order", on_press=lambda button: asyncio.create_task(submit_order(button))))
        body.append(urwid.Button("Exit", on_press=exit_program))
        return urwid.ListBox(urwid.SimpleFocusListWalker(body))

    def broker_toggle(checkbox, state):
        broker = checkbox.get_label()
        if state:
            if broker not in selected_brokers:
                selected_brokers.append(broker)
        else:
            if broker in selected_brokers:
                selected_brokers.remove(broker)
        print(f"Toggle: {broker}, State: {state}, Selected brokers: {selected_brokers}")

    def edit_ticker(edit, content):
        order_details["ticker"] = content.upper()

    def edit_quantity(edit, content):
        try:
            order_details["quantity"] = int(content)
        except ValueError:
            order_details["quantity"] = None

    def edit_price(edit, content):
        try:
            order_details["price"] = float(content)
        except ValueError:
            order_details["price"] = None
    
    def exit_program(button):
        raise urwid.ExitMainLoop()

    async def submit_order(button):
        global selected_brokers
        if not selected_brokers:
            response.set_text("No brokers selected!")
            return
        if not all([order_details["action"], order_details["ticker"], order_details["quantity"]]):
            response.set_text("Please fill in all order details (Buy/Sell, Ticker, Quantity)!")
            return

        brokers_str = ", ".join(selected_brokers)
        response.set_text(f"Submitting order to: {brokers_str}")
        print(f"Final selected brokers: {selected_brokers}")
        print(f"Order details: {order_details}")

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
            "Chase": chaseTrade
        }

        for broker in selected_brokers:
            trade_function = trade_functions.get(broker)
            print(f"Submitting order to {broker}")
            if trade_function:
                try:
                    if broker == "Firstrade":
                        await trade_function(
                            order_details['action'],
                            order_details['quantity'],
                            order_details['ticker']
                        )
                    else:
                        await trade_function(
                            order_details['action'],
                            order_details['quantity'],
                            order_details['ticker'],
                            order_details['price']
                        )
                except Exception as e:
                    print(f"Error submitting order to {broker}: {str(e)}")
                    traceback.print_exc()

        response.set_text("All orders processed. Check console for details.")

    response = urwid.Text("Select brokers and submit orders!!!")
    main = urwid.Padding(menu(u'Brokers:', brokers), left=2, right=2)
    top = urwid.Frame(body=main, footer=urwid.Padding(response, left=2, right=2))
    urwid.MainLoop(top, palette=[('reversed', 'standout', ''), ('editcp', 'light gray', 'dark blue')]).run()

# script.py buy/sell qty ticker price(optional, if given, order is a limit order, otherwise it is a market order)
async def main():
    parser = argparse.ArgumentParser(description="A one click solution to submitting an order across multiple brokers")
    parser.add_argument('action', choices=['buy', 'sell', 'setup'], nargs='?', help='Action to perform')
    parser.add_argument('quantity', type=int, nargs='?', help='Quantity to trade')
    parser.add_argument('ticker', nargs='?', help='Ticker symbol')
    parser.add_argument('price', nargs='?', type=float, help='Price for limit order (optional)')
    args = parser.parse_args()

    if not any([args.action, args.quantity, args.ticker]):
        run_tui()
        return

    if args.action == 'setup':
        setup()
        print("Credentials setup complete. Please rerun the script with trade details.")
        return

    if not all([args.quantity, args.ticker]):
        parser.error("Quantity and ticker are required for buy/sell actions")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(robinTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(tradierTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(tastyTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(publicTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(fennelTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(firstradeTrade(args.action, args.quantity, args.ticker)),
        tg.create_task(schwabTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(bbaeTrade(args.action, args.quantity, args.ticker, args.price)),
        tg.create_task(dspacTrade(args.action, args.quantity, args.ticker, args.price)),


if __name__ == "__main__":
    asyncio.run(main())
