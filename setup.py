import os
from dotenv import load_dotenv
from pathlib import Path
from dump_env import dumper

dotenv_path = Path('.') / '.env'
load_dotenv(dotenv_path=dotenv_path)


def setup():
    print("Setting up broker credentials")
    print("-" * 10 + "Alpaca" + "-" * 10)
    ALPACA_ACCESS_KEY_ID = input("Alpaca Access Key: ")
    ALPACA_SECRET_ACCESS_KEY = input("Alpaca Secret Key: ")

    os.environ["SSG_ALPACA_ACCESS_KEY_ID"] = ALPACA_ACCESS_KEY_ID or os.getenv("ALPACA_ACCESS_KEY_ID") or ""
    os.environ["SSG_ALPACA_SECRET_ACCESS_KEY"] = ALPACA_SECRET_ACCESS_KEY or os.getenv("ALPACA_SECRET_ACCESS_KEY") or ""

    print("-" * 5 + "Saving credentials to .env" + "-" * 5)
    variables = dumper.dump(prefixes=["SSG_"])

    with open(".env", 'w') as f:
        for env_name, env_value in variables.items():
            f.write('{0}={1}\n'.format(env_name, env_value))

    print("Credentials saved to .env")