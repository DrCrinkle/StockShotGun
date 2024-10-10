import os
from dotenv import load_dotenv
from dump_env import dumper

load_dotenv("./.env")


def setup():
    print("Setting up broker credentials, press ENTER to skip entry")

    brokers = {
        "Robinhood": [
            ("ROBINHOOD_USER", "Username"),
            ("ROBINHOOD_PASS", "Password"),
            ("ROBINHOOD_MFA", "MFA"),
        ],
        "Firstrade": [
            ("FIRSTRADE_USER", "Username"),
            ("FIRSTRADE_PASS", "Password"),
            ("FIRSTRADE_PIN", "PIN")
        ],
        "Schwab": [
            ("SCHWAB_API_KEY", "API Key"),
            ("SCHWAB_API_SECRET", "API Secret"),
            ("SCHWAB_CALLBACK_URL", "Callback URL"),
            ("SCHWAB_TOKEN_PATH", "Token Path"),
        ],
        "BBAE": [
            ("BBAE_USER", "Username"),
            ("BBAE_PASS", "Password"),
        ],
        "DSPAC": [
            ("DSPAC_USER", "Username"),
            ("DSPAC_PASS", "Password"),
        ],
        "TastyTrade": [("TASTY_USER", "Username"), ("TASTY_PASS", "Password")],
        "Tradier": [("TRADIER_ACCESS_TOKEN", "Access Token")],
        "Public": [("PUBLIC_USER", "Username"), ("PUBLIC_PASS", "Password")],
        "Fennel": [("FENNEL_EMAIL", "Email")],
    }

    for service, credentials in brokers.items():
        print(f"{'-' * 10}{service}{'-' * 10}")
        for env_var, prompt in credentials:
            value = input(f"{service} {prompt}: ") or os.getenv(env_var) or ""
            os.environ[f"SSG_{env_var}"] = value

    print(f"{'-' * 5} Saving credentials to .env {'-' * 5}")
    variables = dumper.dump(prefixes=["SSG_"])

    with open(".env", 'w') as f:
        for env_name, env_value in variables.items():
            f.write(f'{env_name}={env_value}\n')

    print("Credentials saved to .env")
