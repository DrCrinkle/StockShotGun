import os
import pyotp
import robin_stocks.robinhood as rh
from dotenv import load_dotenv

load_dotenv("./.env")

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
