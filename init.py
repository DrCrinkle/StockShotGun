import os
import pyotp
import alpaca_trade_api as tradeapi
import robin_stocks.robinhood as rh
from dotenv import load_dotenv
from pathlib import Path

dotenv_path = Path('.') / '.env'
load_dotenv(dotenv_path=dotenv_path)


def initAlpaca():
    ALPACA_ACCESS_KEY_ID = os.getenv("ALPACA_ACCESS_KEY_ID")
    ALPACA_SECRET_ACCESS_KEY = os.getenv("ALPACA_SECRET_ACCESS_KEY")

    if not (ALPACA_ACCESS_KEY_ID or ALPACA_SECRET_ACCESS_KEY):
        print("No Alpaca credentials supplied, skipping")
        return None

    # Set up alpaca
    alpaca = tradeapi.REST(
        ALPACA_ACCESS_KEY_ID,
        ALPACA_SECRET_ACCESS_KEY,
        "https://paper-api.alpaca.markets"
    )

    return alpaca


def initRobinHood():
    ROBINHOOD_USER = os.getenv("ROBINHOOD_USER")
    ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS")
    ROBINHOOD_MFA = os.getenv("ROBINHOOD_MFA")

    if not (ROBINHOOD_USER or ROBINHOOD_PASS or ROBINHOOD_MFA):
        print("No Robinhood credentials supplied, skipping")
        return None

    # set up robinhood
    mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
    rh.login(ROBINHOOD_USER, ROBINHOOD_PASS, mfa_code=mfa)

    return rh
