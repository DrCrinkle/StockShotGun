import os
import requests
import pyotp
import robin_stocks.robinhood as rh
from dotenv import load_dotenv

load_dotenv("./.env")


async def robinTrade(side, qty, ticker, price):
    ROBINHOOD_USER = os.getenv("ROBINHOOD_USER")
    ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS")
    ROBINHOOD_MFA  = os.getenv("ROBINHOOD_MFA")

    if not (ROBINHOOD_USER or ROBINHOOD_PASS or ROBINHOOD_MFA):
        print("No Robinhood credentials supplied, skipping")
        return None

    # set up robinhood
    mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
    rh.login(ROBINHOOD_USER, ROBINHOOD_PASS, mfa_code=mfa)

    if side == 'buy':
        if price is not None:
            rh.order_buy_limit(symbol=ticker, quantity=qty, limitPrice=price)
        else:
            rh.order_buy_market(symbol=ticker, quantity=qty)
        print(f"Bought {ticker} on Robinhood")
    else:
        if price is not None:
            rh.order_sell_limit(symbol=ticker, quantity=qty, limitPrice=price)
        else:
            rh.order_sell_market(symbol=ticker, quantity=qty)
        print(f"Sold {ticker} on Robinhood")


async def tradierTrade(side, qty, ticker, price):
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not (TRADIER_ACCESS_TOKEN):
        print("Missing Tradier credentials, skipping")
        return None

    # get tradier accounts
    response = requests.get('https://api.tradier.com/v1/user/profile',
                            headers = {'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                       'Accept': 'application/json'}
                            ) 

    if response.status_code == 200:
        accounts = response.json().get('profile', {}).get('account', [])
        TRADIER_ACCOUNT_ID = [account['account_number'] for account in accounts]
    else:
        # Handle errors (e.g., invalid token, no access to the account)
        print(f"Error: {response.status_code} - {response.text}")
        return False


    if price is not None:
        for i in range(len(TRADIER_ACCOUNT_ID)):
            response = requests.post(f'https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT_ID[i]}/orders',
                                    data = {'class': 'equity',
                                            'symbol': f'{ticker}',
                                            'side': f'{side}',
                                            'quantity': f'{qty}',
                                            'type': 'limit',
                                            'duration': 'day',
                                            'price': f'{price}'},
                                    headers = {'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                                'Accept': 'application/json'}
                                    )
            if side == "buy":
                print(f"Bought {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")
            else:
                print(f"Sold {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")
    else:
        for i in range(len(TRADIER_ACCOUNT_ID)):
            response = requests.post(f'https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT_ID[i]}/orders',
                                    data = {'class': 'equity',
                                            'symbol': f'{ticker}',
                                            'side': f'{side}',
                                            'quantity':  f'{qty}',
                                            'type': 'market',
                                            'duration': 'day'},
                                    headers = {'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                                'Accept': 'application/json'}
                                    )
            if side == "buy":
                print(f"Bought {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")
            else:
                print(f"Sold {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")

async def stockTwitTrade(side, qty, ticker, price):
    STOCKTWITS_ACCESS_TOKEN = os.getenv("STOCKTWITS_ACCESS_TOKEN")

    if not STOCKTWITS_ACCESS_TOKEN:
        print("Missing StockTwits credentials, skipping")
        return None
    
    if price is not None:
        response = requests.post('https://trade-api.stinvest.co/api/v1/trading/orders',
                                json = {'asset_class': 'equities',
                                        'limit_price': f'{price}',
                                        'order_type': 'limit',
                                        'quantity': f'{qty}',
                                        'symbol': f'{ticker}',
                                        'time_in_force': 'DAY',
                                        'transaction_type': f'{side}'},
                                headers = {'Authorization': f'Bearer {STOCKTWITS_ACCESS_TOKEN}',
                                            'Accept': 'application/json'}
                                )
        if response.status_code == 401:
            raise Exception("StockTwits: 401 Unauthorized: Is your access token correct?")
        if side == "buy":
            print(f"Bought {ticker} on StockTwits")
        else:
            print(f"Sold {ticker} on StockTwits")
    else:
        response = requests.post('https://trade-api.stinvest.co/api/v1/trading/orders',
                                json = {'asset_class': 'equities',
                                        'order_type': 'market',
                                        'quantity': f'{qty}',
                                        'symbol': f'{ticker}',
                                        'time_in_force': 'DAY',
                                        'transaction_type': f'{side}'},
                                headers = {'Authorization': f'Bearer {STOCKTWITS_ACCESS_TOKEN}',
                                            'Accept': 'application/json'}
                                )
        if response.status_code == 401:
            raise Exception("StockTwits: 401 Unauthorized: Is your access token correct?")
        if side == "buy":
            print(f"Bought {ticker} on StockTwits")
        else:
            print(f"Sold {ticker} on StockTwits")

#TODO: Implement Webull Trading
#async def webullTrade():
    # if price is lower than $1, buy 100 shares and sell 99, to get around webull restrictions