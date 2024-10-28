import argparse
import asyncio
from setup import setup
from tui import run_tui
from brokers import (
    robinTrade, tradierTrade, tastyTrade, publicTrade, 
    firstradeTrade, fennelTrade, schwabTrade, bbaeTrade, 
    dspacTrade
)

async def run_cli(args, parser):
    if args.action == 'setup':
        setup()
        print("Credentials setup complete. Please rerun the script with trade details.")
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
    parser.add_argument('action', choices=['buy', 'sell', 'setup'], nargs='?', help='Action to perform')
    parser.add_argument('quantity', type=int, nargs='?', help='Quantity to trade')
    parser.add_argument('ticker', nargs='?', help='Ticker symbol')
    parser.add_argument('price', nargs='?', type=float, help='Price for limit order (optional)')
    args = parser.parse_args()

    if not any([args.action, args.quantity, args.ticker]):
        run_tui()
    else:
        asyncio.run(run_cli(args, parser))

if __name__ == "__main__":
    main()
