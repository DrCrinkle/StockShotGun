import os
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from pathlib import Path

dotenv_path = Path('.') / '.env'
load_dotenv(dotenv_path=dotenv_path)


def initAlpaca():
    ALPACA_ACCESS_KEY_ID = os.getenv("ALPACA_ACCESS_KEY_ID")
    ALPACA_SECRET_ACCESS_KEY = os.getenv("ALPACA_SECRET_ACCESS_KEY")
    if len(ALPACA_ACCESS_KEY_ID) <= 0 or len(ALPACA_SECRET_ACCESS_KEY) <= 0:
        print("No Alpaca credentials supplied, skipping")
        return None
    # Set up alpaca
    alpaca = tradeapi.REST(
        ALPACA_ACCESS_KEY_ID,
        ALPACA_SECRET_ACCESS_KEY,
        "https://paper-api.alpaca.markets"
    )

    return alpaca
