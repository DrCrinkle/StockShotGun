import argparse
import asyncio
from setup import setup
from tui import run_tui
from brokers import session_manager, BrokerConfig
from tui.broker_functions import BROKER_CONFIG as BROKER_FUNCTIONS
from order_processor import order_processor

async def print_holdings(holdings):
    """Print holdings in a formatted way."""
    if holdings:
        for account, positions in holdings.items():
            print(f"\nAccount: {account}")
            if not positions:
                print("No positions found")
            for pos in positions:
                symbol = pos.get('symbol', 'N/A')
                quantity = pos.get('quantity', 0)

                cost_basis = pos.get('cost_basis')
                if cost_basis is None:
                    cost_basis_display = "N/A"
                else:
                    cost_basis_display = f"${float(cost_basis):.2f}"

                current_value = pos.get('current_value')
                if current_value is None:
                    fallback_value = pos.get('value')
                    if fallback_value is None and pos.get('price') is not None:
                        try:
                            fallback_value = float(pos['price']) * float(quantity)
                        except (TypeError, ValueError):
                            fallback_value = None
                    current_value = fallback_value

                if current_value is None:
                    current_value_display = "N/A"
                else:
                    current_value_display = f"${float(current_value):.2f}"

                print(
                    f"\nSymbol: {symbol}\n"
                    f"Quantity: {quantity}\n"
                    f"Cost Basis: {cost_basis_display}\n"
                    f"Current Value: {current_value_display}"
                )

async def run_cli(args, parser):
    if args.action == "setup":
        setup()
        print("Credentials setup complete. Please rerun the script with trade details.")
        return

    if args.action == "holdings":
        if not args.broker:
            parser.error("--broker is required for holdings action")
            return
        broker = args.broker[0]  # For holdings, use the first specified broker
        if broker not in BROKER_FUNCTIONS:
            parser.error("Invalid broker specified for holdings")
            return

        # Initialize only the selected broker
        await session_manager.initialize_selected_sessions([broker])
        holdings_func = BROKER_FUNCTIONS[broker]["holdings"]
        holdings = await holdings_func(args.ticker)
        await print_holdings(holdings)
        return

    if not all([args.quantity, args.ticker]):
        parser.error("Quantity and ticker are required for buy/sell actions")

    # Determine which brokers to use
    if args.broker:
        # Use only the specified broker(s)
        brokers_to_use = args.broker
        # Validate that all specified brokers are available
        for broker_name in brokers_to_use:
            if broker_name not in BROKER_FUNCTIONS:
                parser.error(f"Invalid broker specified: {broker_name}")
                return
    else:
        # If no broker specified, use all available brokers
        brokers_to_use = []
        for broker_name in BrokerConfig.get_all_brokers():
            if broker_name in BROKER_FUNCTIONS:
                brokers_to_use.append(broker_name)

        if not brokers_to_use:
            parser.error("No broker credentials configured")
            return

    # Initialize only the brokers we're going to use
    await session_manager.initialize_selected_sessions(brokers_to_use)

    # Build trade functions dict for order processor
    trade_functions = {
        broker_name: BROKER_FUNCTIONS[broker_name]["trade"]
        for broker_name in brokers_to_use
        if broker_name in BROKER_FUNCTIONS and "trade" in BROKER_FUNCTIONS[broker_name]
    }

    # Create order for the processor
    order = {
        "action": args.action,
        "quantity": args.quantity,
        "ticker": args.ticker,
        "price": args.price,
        "selected_brokers": brokers_to_use
    }

    # Use order processor for concurrent execution with better error handling
    print(f"\n{args.action.upper()} {args.quantity} {args.ticker} @ ${args.price if args.price else 'market'}")
    print(f"Executing across {len(brokers_to_use)} broker(s): {', '.join(brokers_to_use)}\n")

    # Wrapper function for CLI mode that ignores force_redraw parameter
    def cli_response_fn(message, force_redraw=False):
        if message:  # Only print non-empty messages
            print(message)

    results = await order_processor.process_orders(
        [order],
        trade_functions,
        cli_response_fn  # Use wrapper that handles force_redraw parameter
    )

    # Print summary
    print(f"\n{'='*60}")
    print("Order execution complete:")
    print(f"  Successful brokers: {len([s for status in results['statuses'] for s in status['successful']])}")
    print(f"  Failed brokers: {len([f for status in results['statuses'] for f in status['failed']])}")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="A one click solution to submitting an order across multiple brokers")
    parser.add_argument("action", choices=["buy", "sell", "setup", "holdings"], nargs="?", help="Action to perform")
    parser.add_argument("quantity", type=int, nargs="?", help="Quantity to trade")
    parser.add_argument("ticker", nargs="?", help="Ticker symbol")
    parser.add_argument("price", nargs="?", type=float, help="Price for limit order (optional)")
    parser.add_argument("--broker", action="append", help="Broker(s) to use. Can be specified multiple times (e.g., --broker Public --broker Robinhood)")
    args = parser.parse_args()

    try:
        if not any([args.action, args.quantity, args.ticker]):
            run_tui()
        else:
            asyncio.run(run_cli(args, parser))
    finally:
        session_manager.cleanup()


if __name__ == "__main__":
    main()
