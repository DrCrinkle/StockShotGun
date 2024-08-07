import os
import requests
import pyotp
import robin_stocks.robinhood as rh
from firstrade import account as ft_account, order, symbols
from public_invest_api import Public
from decimal import Decimal
from tastytrade import ProductionSession, Account
from tastytrade.instruments import Equity
from tastytrade.order import NewOrder, OrderTimeInForce, OrderType, PriceEffect, OrderAction
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
            order_args = {'symbol': ticker, 'quantity': qty, 'account_number': account_number}
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


async def tastyTrade(side, qty, ticker, price):
    TASTY_USER = os.getenv("TASTY_USER")
    TASTY_PASS = os.getenv("TASTY_PASS")

    if not (TASTY_USER or TASTY_PASS):
        print("No TastyTrade credentials supplied, skipping")
        return None

    session = ProductionSession(TASTY_USER, TASTY_PASS)
    accounts = Account.get_accounts(session)
    symbol = Equity.get_equity(session, ticker)
    action = OrderAction.BUY_TO_OPEN if side == "buy" else OrderAction.SELL_TO_CLOSE

    # Build the order
    leg = symbol.build_leg(Decimal(qty), action)
    order_type = OrderType.LIMIT if price else OrderType.MARKET
    price_effect = PriceEffect.DEBIT if side == "buy" else PriceEffect.CREDIT
    order_args = {
        "time_in_force": OrderTimeInForce.DAY,
        "order_type": order_type,
        "legs": [leg],
        "price_effect": price_effect,
    }
    if price:
        order_args["price"] = price
    
    order = NewOrder(**order_args)

    for acc in accounts:
        placed_order = acc.place_order(session, order, dry_run = False)
        order_status = placed_order.order.status.value

        if order_status in ["Received", "Routed"]:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on TastyTrade {acc.account_type_name} account {acc.account_number}")

async def publicTrade(side, qty, ticker, price):
    PUBLIC_USER = os.getenv("PUBLIC_USER")
    PUBLIC_PASS = os.getenv("PUBLIC_PASS")

    if not (PUBLIC_USER or PUBLIC_PASS):
        print("No Public credentials supplied, skipping")
        return None

    public = Public()
    public.login(
        username=PUBLIC_USER,
        password=PUBLIC_PASS,
        wait_for_2fa=True # When logging in for the first time, you need to wait for the SMS code
    )

    order = public.place_order(
        symbol=ticker,
        quantity=qty,
        side=side,
        order_type='MARKET',
        time_in_force='DAY',
        tip=0 # The amount to tip Public.com
    )
    if order["success"] is True:
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {ticker} on Public")


def firstradeTrade(side, qty, ticker):
    FIRSTRADE_USER = os.getenv("FIRSTRADE_USER")
    FIRSTRADE_PASS = os.getenv("FIRSTRADE_PASS")
    FIRSTRADE_PIN = os.getenv("FIRSTRADE_PIN")

    ft_ss = ft_account.FTSession(
        username=FIRSTRADE_USER, password=FIRSTRADE_PASS, pin=FIRSTRADE_PIN
    )

    ft_accounts = ft_account.FTAccountData(ft_ss)

    symbol_data = symbols.SymbolQuote(ft_ss, ticker)
    if symbol_data.last < 1.00:
        price_type = order.PriceType.LIMIT
        if side == "buy":
            price = symbol_data.bid + 0.01
        else:
            price = symbol_data.ask - 0.01

    ft_order = order.Order(ft_ss)

    for account_number in ft_accounts.account_numbers:
        ft_order.place_order(
            account_number,
            symbol=ticker,
            price_type=price_type,
            order_type=order.OrderType.BUY if side == "buy" else order.OrderType.SELL,
            quantity=qty,
            duration=order.Duration.DAY,
            price=price,
            dry_run=False,
        )

        # Check if order was successful
        if ft_order.order_confirmation["success"] == "Yes":
            print(f"Order for {ticker} placed on Firstrade successfully.")
            # Print Order ID
            print(f"Order ID: {ft_order.order_confirmation['orderid']}.")
        else:
            print(f"Failed to place order for {ticker} on Firstrade.")
            # Print errormessage
            print(ft_order.order_confirmation["actiondata"])


# TODO: Implement Webull Trading
# async def webullTrade():
# if price is lower than $1, buy 100 shares and sell 99, to get around webull restrictions
