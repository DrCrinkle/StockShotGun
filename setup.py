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
    TRADIER_ACCESS_TOKEN = input("Tradier Access Token: ") or os.getenv("TRADIER_ACCESS_TOKEN")
    TRADIER_ACCOUNT_ID = []

    try:
        num = int(input("How many Tradier accounts?: "))
    except ValueError:
        num = None

    if num is not None:
        for i in range(num):
            if num == 1:
                account_id = input("Tradier Account ID: ") or os.getenv("TRADIER_ACCOUNT_ID")
                TRADIER_ACCOUNT_ID.append(account_id)
                os.environ["SSG_TRADIER_ACCOUNT_ID"] = account_id
            else:
                account_id = input(f"Tradier Account {i} ID: ") or os.getenv(f"TRADIER_ACCOUNT{i}_ID")
                TRADIER_ACCOUNT_ID.append(account_id)
                os.environ[f"SSG_TRADIER_ACCOUNT{i}_ID"] = account_id
    else:
        # Load and preserve existing account IDs from .env
        existing_account_id = os.getenv("TRADIER_ACCOUNT_ID")
        if existing_account_id:
            TRADIER_ACCOUNT_ID.append(existing_account_id)
            os.environ["SSG_TRADIER_ACCOUNT_ID"] = existing_account_id
        else:
            i = 0
            while True:
                existing_account_id = os.getenv(f"TRADIER_ACCOUNT{i}_ID")
                if existing_account_id:
                    TRADIER_ACCOUNT_ID.append(existing_account_id)
                    os.environ[f"SSG_TRADIER_ACCOUNT{i}_ID"] = existing_account_id
                    i += 1
                else:
                    break

    os.environ["SSG_TRADIER_ACCESS_TOKEN"] = TRADIER_ACCESS_TOKEN

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
