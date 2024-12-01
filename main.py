import argparse
import asyncio
from setup import setup
from tui import run_tui
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


async def run_cli(args, parser):
    if args.action == "setup":
        setup()
        print("Credentials setup complete. Please rerun the script with trade details.")
        return

    if args.action == "holdings":

        async def print_holdings(holdings):
            if holdings:
                for account, positions in holdings.items():
                    print(f"\nAccount: {account}")
                    if not positions:
                        print("No positions found")
                    for pos in positions:
                        print(f"\nSymbol: {pos['symbol']}")
                        print(f"Quantity: {pos['quantity']}")
                        print(f"Cost Basis: ${pos['cost_basis']:.2f}")
                        print(f"Current Value: ${pos['current_value']:.2f}")

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

        broker = args.broker
        if broker in holdings_functions:
            holdings = await holdings_functions[broker](args.ticker)
            await print_holdings(holdings)
        else:
            parser.error("Invalid broker specified for holdings")
        return

    if not all([args.quantity, args.ticker]):
        parser.error("Quantity and ticker are required for buy/sell actions")

    async with asyncio.TaskGroup() as tg:
        tg.create_task(robinTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(tradierTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(tastyTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(publicTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(fennelTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(firstradeTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(schwabTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(bbaeTrade(args.action, args.quantity, args.ticker, args.price))
        tg.create_task(dspacTrade(args.action, args.quantity, args.ticker, args.price))


def main():
    parser = argparse.ArgumentParser(description="A one click solution to submitting an order across multiple brokers")
    parser.add_argument("action", choices=["buy", "sell", "setup", "holdings"], nargs="?", help="Action to perform")
    parser.add_argument("quantity", type=int, nargs="?", help="Quantity to trade")
    parser.add_argument("ticker", nargs="?", help="Ticker symbol")
    parser.add_argument("price", nargs="?", type=float, help="Price for limit order (optional)")
    parser.add_argument("--broker", help="Broker to check holdings for (required for holdings action)")
    args = parser.parse_args()

    if not any([args.action, args.quantity, args.ticker]):
        run_tui()
    else:
        asyncio.run(run_cli(args, parser))


if __name__ == "__main__":
    main()
