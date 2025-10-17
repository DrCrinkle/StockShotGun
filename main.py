import argparse
import asyncio
from setup import setup
from tui import run_tui
from brokers import session_manager, BrokerConfig
from tui.broker_functions import BROKER_CONFIG as BROKER_FUNCTIONS

async def print_holdings(holdings):
    """Print holdings in a formatted way."""
    if holdings:
        for account, positions in holdings.items():
            print(f"\nAccount: {account}")
            if not positions:
                print("No positions found")
            for pos in positions:
                print(
                    f"\nSymbol: {pos['symbol']}\n"
                    f"Quantity: {pos['quantity']}\n"
                    f"Cost Basis: ${pos['cost_basis']:.2f}\n"
                    f"Current Value: ${pos['current_value']:.2f}"
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

    # Create trading tasks for the selected brokers
    trading_tasks = []
    for broker_name in brokers_to_use:
        trade_func = BROKER_FUNCTIONS[broker_name]["trade"]
        task = asyncio.create_task(
            trade_func(args.action, args.quantity, args.ticker, args.price)
        )
        trading_tasks.append(task)
    
    # Execute all trading tasks concurrently
    if trading_tasks:
        await asyncio.gather(*trading_tasks, return_exceptions=True)


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
