import os
from dotenv import load_dotenv
from dump_env import dumper

load_dotenv("./.env")


def setup():
    print("Setting up broker credentials, press ENTER to skip entry")

    #Alpaca
    print("-" * 10 + "Alpaca" + "-" * 10)
    ALPACA_ACCESS_KEY_ID = input("Alpaca Access Key: ")
    ALPACA_SECRET_ACCESS_KEY = input("Alpaca Secret Key: ")

    os.environ["SSG_ALPACA_ACCESS_KEY_ID"] = ALPACA_ACCESS_KEY_ID or os.getenv("ALPACA_ACCESS_KEY_ID") or ""
    os.environ["SSG_ALPACA_SECRET_ACCESS_KEY"] = ALPACA_SECRET_ACCESS_KEY or os.getenv("ALPACA_SECRET_ACCESS_KEY") or ""

    #Robinhood
    print("-" * 10 + "Robinhood" + "-" * 10)
    ROBINHOOD_USER = input("Robinhood Username: ")
    ROBINHOOD_PASS = input("Robinhood Password: ")
    ROBINHOOD_MFA = input("Robinhood MFA: ")

    os.environ["SSG_ROBINHOOD_USER"] = ROBINHOOD_USER or os.getenv("ROBINHOOD_USER") or ""
    os.environ["SSG_ROBINHOOD_PASS"] = ROBINHOOD_PASS or os.getenv("ROBINHOOD_PASS") or ""
    os.environ["SSG_ROBINHOOD_MFA"] = ROBINHOOD_MFA or os.getenv("ROBINHOOD_MFA") or ""

    #Tradier
    print("-" * 10 + "Tradier" + "-" * 10)
    TRADIER_ACCESS_TOKEN = input("Tradier Access Token: ")
    TRADIER_ACCOUNT_ID = []

    # i don't like this if statment
    num = int(input("How many Tradier accounts?: "))
    if num == 1:
        TRADIER_ACCOUNT_ID.append(input("Tradier Account ID: "))
        os.environ["SSG_TRADIER_ACCOUNT_ID"] = TRADIER_ACCOUNT_ID[0] or os.getenv("TRADIER_ACCOUNT_ID") or ""
    else:
        for i in range(num):
            TRADIER_ACCOUNT_ID.append(input(f"Tradier Account{i} ID: "))
            os.environ[f"SSG_TRADIER_ACCOUNT{i}_ID"] = TRADIER_ACCOUNT_ID[i] or os.getenv("TRADIER_ACCOUNT_ID") or os.getenv(f"TRADIER_ACCOUNT{i}_ID")  or ""

    os.environ["SSG_TRADIER_ACCESS_TOKEN"] = TRADIER_ACCESS_TOKEN or os.getenv("TRADIER_ACCESS_TOKEN") or ""

    #Stocktwits
    print("-" * 10 + "StockTwits" + "-" * 10)
    STOCKTWITS_ACCESS_TOKEN = input("StockTwits Access Token: ")

    os.environ["SSG_STOCKTWITS_ACCESS_TOKEN"] = STOCKTWITS_ACCESS_TOKEN or os.getenv("STOCKTWITS_ACCESS_TOKEN") or ""

    #Save credentials
    print("-" * 5 + "Saving credentials to .env" + "-" * 5)
    variables = dumper.dump(prefixes=["SSG_"])

    with open(".env", 'w') as f:
        for env_name, env_value in variables.items():
            f.write('{0}={1}\n'.format(env_name, env_value))

    print("Credentials saved to .env")
