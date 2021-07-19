from init import initAlpaca
from setup import setup
from brokers import alpacaTrade
from sys import argv

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

# could use a match case statement here but im not using python 3.10 :/
if side == "buy" or side == "sell":
    alpaca = initAlpaca()
    alpacaTrade(side, qty, ticker, price, alpaca)
elif side == "setup":
    print("""
          Great, now that credentials are setup, please rerun the script with the below usage

          Usage: buy/sell/setup quantity ticker price(optional)
          """)
else:
    print("""
          Invalid Argument 
          
          Usage: buy/sell/setup quantity ticker price(optional)
          """)
    exit()
