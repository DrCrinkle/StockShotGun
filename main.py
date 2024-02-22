import requests
import asyncio
from sys import argv
from brokers import alpacaTrade, robinTrade, tradierTrade, stockTwitTrade
from setup import setup

# script.py buy/sell qty ticker price(optional, if given, order is a limit order, otherwise it is a market order)
async def main():
    if len(argv) == 1:
        print("""
            A one click solution to submitting an order across multiple brokers
            
            Usage: buy/sell/setup quantity ticker price(optional)
            """)
        exit()

    # parse arguments
    try:
        side = argv[1]
        qty = int(argv[2])
        ticker = argv[3]
        if len(argv) == 5:
            price = argv[4]
        else:
            price = None
    except IndexError:
        if side == "setup":
            setup()
        else:
            print("""
                Missing Arguments
                
                Usage: buy/sell/setup quantity ticker price(optional)
                """)
            exit()

    if side == "setup":
        print("""
            Great, now that credentials are setup, please rerun the script with the below usage

            Usage: buy/sell/setup quantity ticker price(optional)
            """)
        exit()

    # using Tradier sandbox to get exchange data
    response = requests.get('https://sandbox.tradier.com/v1/markets/quotes',
                            params={'symbols': {ticker}},
                            headers={'Authorization': 'Bearer 3AWwPaG2P5jqLgTLqdTYuU928qbx',
                                    'Accept': 'application/json'})

    json_response = response.json()


    match side:
        case "buy" | "sell":
            if json_response['quotes']['quote']['exch'] == "V":
                print("Trading OTC")
            else:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(alpacaTrade(side, qty, ticker, price))
                    tg.create_task(robinTrade(side, qty, ticker, price))
                    tg.create_task(tradierTrade(side, qty, ticker, price))
                    tg.create_task(stockTwitTrade(side, qty, ticker, price))
        case _ :
            print("""
                Invalid Argument 
                
                Usage: buy/sell/setup quantity ticker price(optional)
                """)
            exit()

if __name__ == "__main__":
    asyncio.run(main())