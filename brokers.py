import requests
import os
from dotenv import load_dotenv
from pathlib import Path

dotenv_path = Path('.') / '.env'
load_dotenv(dotenv_path=dotenv_path)


def alpacaTrade(side, qty, ticker, price, alpaca):
    try:
        if price is not None:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='limit',
                                time_in_force='day',
                                limit_price=price)
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
        else:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='market',
                                time_in_force='day')
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
    except:
        return False

    return True


def robinTrade(side, qty, ticker, price, rh):
    try:
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
    except:
        return False
    return True


def tradierTrade(side, qty, ticker, price):
    TRADIER_ACCOUNT_ID = os.getenv("TRADIER_ACCOUNT_ID")
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    try:
        if price is not None:
            response = requests.post(f'https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT_ID}/orders',
                                     data={'class': 'equity', 'symbol': f'{ticker}', 'side': f'{side}',
                                           'quantity': f'{qty}', 'type': 'limit', 'duration': 'day',
                                           'price': f'{price}'},
                                     headers={'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                              'Accept': 'application/json'}
                                     )
            if side == "buy":
                print(f"Bought {ticker} on Tradier")
            else:
                print(f"Sold {ticker} on Tradier")
        else:
            response = requests.post(f'https://api.tradier.com/v1/accounts/{TRADIER_ACCOUNT_ID}/orders',
                                     data={'class': 'equity', 'symbol': f'{ticker}', 'side': f'{side}',
                                           'quantity':  f'{qty}', 'type': 'market', 'duration': 'day'},
                                     headers={'Authorization': f'Bearer {TRADIER_ACCESS_TOKEN}',
                                              'Accept': 'application/json'}
                                     )
            if side == "buy":
                print(f"Bought {ticker} on Tradier")
            else:
                print(f"Sold {ticker} on Tradier")
    except:
        return False
    return True
