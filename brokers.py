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

    all_accounts = rh.account.load_account_profile(dataType="results")

    for account in all_accounts:
        account_number = account['account_number']
        brokerage_account_type = account['brokerage_account_type']

        order_function = None
        if side == 'buy':
            order_function = rh.order_buy_limit if price else rh.order_buy_market
        elif side == 'sell':
            order_function = rh.order_sell_limit if price else rh.order_sell_market

        if order_function:
            order_args = {'symbol': ticker, 'quantity': qty}
            if price:
                order_args['limitPrice'] = price

            order_function(**order_args)
            action_str = "Bought" if side == "buy" else "Sold"
            
            print(f"{action_str} {ticker} on Robinhood {brokerage_account_type} account {account_number}")

async def tradierTrade(side, qty, ticker, price):
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not TRADIER_ACCESS_TOKEN:
        print("Missing Tradier credentials, skipping")
        return None

    # Get Tradier accounts
    response = requests.get('https://api.tradier.com/v1/user/profile',
                            headers={'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                     'Accept': 'application/json'})

    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return False

    accounts = response.json().get('profile', {}).get('account', [])
    if not accounts:
        print("No accounts found.")
        return False

    TRADIER_ACCOUNT_ID = [account['account_number'] for account in accounts]

    # Order placement
    order_type = 'limit' if price else 'market'
    price_data = {'price': f'{price}'} if price else {}

    for account_id in TRADIER_ACCOUNT_ID:
        response = requests.post(f'https://api.tradier.com/v1/accounts/{account_id}/orders',
                                 data={'class': 'equity',
                                       'symbol': ticker,
                                       'side': side,
                                       'quantity': qty,
                                       'type': order_type,
                                       'duration': 'day',
                                       **price_data},
                                 headers={'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                          'Accept': 'application/json'})
        
        if response.status_code != 200:
            print(f"Error placing order on account {account_id}: {response.text}")
        else:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on Tradier account {account_id}")

async def stockTwitTrade(side, qty, ticker, price):
    STOCKTWITS_ACCESS_TOKEN = os.getenv("STOCKTWITS_ACCESS_TOKEN")

    if not STOCKTWITS_ACCESS_TOKEN:
        print("Missing StockTwits credentials, skipping")
        return None
    
    order_type = 'limit' if price else 'market'
    price_data = {'limit_price': f'{price}'} if price else {}
    
    response = requests.post('https://trade-api.stinvest.co/api/v1/trading/orders',
                            json = {'asset_class': 'equities',
                                    'symbol': ticker,
                                    'quantity': str(qty),
                                    'order_type': order_type,
                                    'time_in_force': 'DAY',
                                    'transaction_type': side,
                                    **price_data},
                            headers = {'Authorization': f'Bearer {STOCKTWITS_ACCESS_TOKEN}',
                                        'Accept': 'application/json'}
                            )
    if response.status_code == 401:
        raise Exception("StockTwits: 401 Unauthorized: Check your access token")
    
    if response.ok:
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {ticker} on StockTwits")
    else:
        print(f"Error {response.status_code}: {response.text}")


#TODO: Implement Webull Trading
#async def webullTrade():
    # if price is lower than $1, buy 100 shares and sell 99, to get around webull restrictions