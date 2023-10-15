import requests
import asyncio
from sys import argv
from brokers import allyTrade, alpacaTrade, robinTrade, tradierTrade, stockTwitTrade
from init import initRobinHood
from setup import setup

# script.py buy/sell qty ticker price(optional, if given, order is a limit order, otherwise it is a market order)
if len(argv) == 1:
    print("""
          A one click solution to submitting an order across multiple brokers
          
          Usage: buy/sell/setup quantity ticker 
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
                        headers={'Authorization': 'Bearer 3AWwPaG2P5jqLgTLqdTYuU928qbx', 'Accept': 'application/json'})

json_response = response.json()

# could use a match case statement here but im not using python 3.10 :/
if (side == "buy" or side == "sell") and json_response['quotes']['quote']['exch'] == "V":
    print("Trading OTC")
elif side == "buy" or side == "sell":
    alpacaTrade(side, qty, ticker, price, initAlpaca())
    robinTrade(side, qty, ticker, price, initRobinHood())
    tradierTrade(side, qty, ticker, price)
    allyTrade(side, qty, ticker, price)
else:
    print("""
          Invalid Argument 
          
          Usage: buy/sell/setup quantity ticker price(optional)
          """)
    exit()
