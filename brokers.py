import os
import ally
import requests
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from dotenv import load_dotenv

load_dotenv("./.env")


async def alpacaTrade(side, qty, ticker, price):
    ALPACA_ACCESS_KEY_ID = os.getenv("ALPACA_ACCESS_KEY_ID")
    ALPACA_SECRET_ACCESS_KEY = os.getenv("ALPACA_SECRET_ACCESS_KEY")

    if not (ALPACA_ACCESS_KEY_ID or ALPACA_SECRET_ACCESS_KEY):
        print("Missing Alpaca credentials, skipping")
        return None
    
    trading_client = TradingClient(ALPACA_ACCESS_KEY_ID, ALPACA_SECRET_ACCESS_KEY, paper=True)

    try:
        if price is not None:
            limit_order_data = LimitOrderRequest(
                                    symbol=ticker,
                                    limit_price=price,
                                    qty=qty,
                                    side=side,
                                    time_in_force='day')
            trading_client.submit_order(order_data=limit_order_data)
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
        else:
            market_order_data = MarketOrderRequest(
                                    symbol=ticker,
                                    qty=qty,
                                    side=side,
                                    time_in_force='day')
            trading_client.submit_order(order_data=market_order_data)
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
    except:
        return False

    return True


async def robinTrade(side, qty, ticker, price, rh):
    if not rh:
        return False
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


async def tradierTrade(side, qty, ticker, price):
    # get tradier accounts
    env = os.environ
    TRADIER_ACCOUNT_ID = []
    for key, value in env.items():
        if key.startswith("TRADIER_ACCOUNT"):
            TRADIER_ACCOUNT_ID.append(value)

    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not (TRADIER_ACCOUNT_ID or TRADIER_ACCESS_TOKEN):
        print("Missing Tradier credentials, skipping")
        return None

    try:
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
                if response.status_code == 401:
                    raise Exception("Tradier: 401 Unauthorized: Is your access token and account id correct?")
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
                if response.status_code == 401:
                    raise Exception("Tradier: 401 Unauthorized: Is your access token and account id correct?")
                if side == "buy":
                    print(f"Bought {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")
                else:
                    print(f"Sold {ticker} on Tradier account {TRADIER_ACCOUNT_ID[i]}")
    except Exception as e:
        print(e)
        return False
    except:
        return False
    return True


async def allyTrade(side, qty, ticker, price):
    try:
        a = ally.Ally()
    except ally.exception.ApiKeyException:
        print("No Ally credentials supplied, skipping")
        return None

    try:
        if price is not None:
            o = ally.Order.Order(
                buysell=side,
                symbol=ticker,
                price=ally.Order.Limit(limpx=price),
                time='day',
                qty=qty
            )
            a.submit(o, preview=False)
            if side == "buy":
                print(f"Bought {ticker} on Ally")
            else:
                print(f"Sold {ticker} on Ally")
            return True
        else:
            o = ally.Order.Order(
                buysell=side,
                symbol=ticker,
                price=ally.Order.Market(),
                time='day',
                qty=qty
            )
            a.submit(o, preview=False)
            if side == "buy":
                print(f"Bought {ticker} on Ally")
            else:
                print(f"Sold {ticker} on Ally")
            return True
    except ally.exception.ExecutionException as e:
        print(f"Ally: {e}")
        return False
