import os
from dotenv import load_dotenv
from dump_env import dumper

load_dotenv("./.env")


def setup():
    print("Setting up broker credentials, press ENTER to skip entry")

    #Robinhood
    print("-" * 10 + "Robinhood" + "-" * 10)
    ROBINHOOD_USER = input("Robinhood Username: ")
    ROBINHOOD_PASS = input("Robinhood Password: ")
    ROBINHOOD_MFA  = input("Robinhood MFA: ")

    os.environ["SSG_ROBINHOOD_USER"] = ROBINHOOD_USER or os.getenv("ROBINHOOD_USER") or ""
    os.environ["SSG_ROBINHOOD_PASS"] = ROBINHOOD_PASS or os.getenv("ROBINHOOD_PASS") or ""
    os.environ["SSG_ROBINHOOD_MFA"] = ROBINHOOD_MFA or os.getenv("ROBINHOOD_MFA") or ""

    #Tradier
    print("-" * 10 + "Tradier" + "-" * 10)
    TRADIER_ACCESS_TOKEN = input("Tradier Access Token: ") or os.getenv("TRADIER_ACCESS_TOKEN")

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
