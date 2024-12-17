import os
import httpx
import pyotp
import traceback
import robin_stocks.robinhood as rh
from bbae_invest_api import BBAEAPI
from dspac_invest_api import DSPACAPI
from firstrade import account as ft_account, order, symbols
from public_invest_api import Public
from fennel_invest_api import Fennel
from decimal import Decimal
from tastytrade import Session, Account
from tastytrade.instruments import Equity
from tastytrade.order import (
    NewOrder,
    OrderTimeInForce,
    OrderType,
    PriceEffect,
    OrderAction,
)
from schwab import auth 
from schwab.orders.equities import (
    equity_buy_limit,
    equity_buy_market,
    equity_sell_limit,
    equity_sell_market,
)
from dotenv import load_dotenv

load_dotenv("./.env")


async def _login_broker(broker_api, broker_name):
    """Helper function to handle login flow for BBAE and DSPAC brokers"""
    try:
        broker_api.make_initial_request()
        login_ticket = broker_api.generate_login_ticket_email()
        
        if login_ticket.get("Data") is None:
            raise Exception("Invalid response from generating login ticket")
            
        if login_ticket.get("Data").get("needSmsVerifyCode", False):
            if login_ticket.get("Data").get("needCaptchaCode", False):
                captcha_image = broker_api.request_captcha()
                captcha_image.save(f"./{broker_name}captcha.png", format="PNG")
                captcha_input = input(
                    f"CAPTCHA image saved to ./{broker_name}captcha.png. Please open it and type in the code: "
                )
                broker_api.request_email_code(captcha_input=captcha_input)
            else:
                broker_api.request_email_code()
                
            otp_code = input(f"Enter {broker_name} security code: ")
            login_ticket = broker_api.generate_login_ticket_email(otp_code)

        login_response = broker_api.login_with_ticket(login_ticket.get("Data").get("ticket"))
        if login_response.get("Outcome") != "Success":
            raise Exception(f"Login failed. Response: {login_response}")
            
        return True
        
    except Exception as e:
        print(f"Error logging into {broker_name}: {str(e)}")
        return False


async def _get_broker_holdings(broker_api, broker_name, ticker=None):
    """Helper function to get holdings for BBAE and DSPAC brokers"""
    try:
        holdings_data = {}
        holdings_response = broker_api.get_account_holdings()
        
        if holdings_response.get("Outcome") != "Success":
            raise Exception(f"Failed to get holdings: {holdings_response.get('Message')}")

        positions = holdings_response.get("Data", [])
        
        if ticker:
            positions = [pos for pos in positions if pos.get("Symbol") == ticker]

        account_info = broker_api.get_account_info()
        account_number = account_info.get("Data").get('accountNumber')

        formatted_positions = [
            {
                "symbol": pos.get("Symbol", "Unknown"),
                "quantity": float(pos.get("CurrentAmount", 0)),
                "cost_basis": float(pos.get("CostPrice", 0)),
                "current_value": float(pos.get("Last", 0)) * float(pos.get("CurrentAmount", 0))
            }
            for pos in positions
            if float(pos.get("CurrentAmount", 0)) > 0
        ]

        holdings_data[account_number] = formatted_positions
        return holdings_data

    except Exception as e:
        print(f"Error retrieving {broker_name} holdings: {str(e)}")
        traceback.print_exc()
        return None


async def robinTrade(side, qty, ticker, price):
    ROBINHOOD_USER = os.getenv("ROBINHOOD_USER")
    ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS")
    ROBINHOOD_MFA  = os.getenv("ROBINHOOD_MFA")

    if not (ROBINHOOD_USER and ROBINHOOD_PASS and ROBINHOOD_MFA):
        print("No Robinhood credentials supplied, skipping")
        return None

    mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
    rh.login(ROBINHOOD_USER, ROBINHOOD_PASS, mfa_code=mfa, pickle_path="./tokens/")

    all_accounts = rh.account.load_account_profile(dataType="results")

    for account in all_accounts:
        account_number = account['account_number']
        brokerage_account_type = account['brokerage_account_type']

        if side == 'buy':
            order_function = rh.order_buy_limit if price else rh.order_buy_market
        elif side == 'sell':
            order_function = rh.order_sell_limit if price else rh.order_sell_market
        else:
            print(f"Invalid side: {side}")
            return None

        order_args = {
            "symbol": ticker,
            "quantity": qty,
            "account_number": account_number,
            "timeInForce": "gfd",
        }
        if price:
            order_args['limitPrice'] = price

        order_function(**order_args)

        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {ticker} on Robinhood {brokerage_account_type} account {account_number}")


async def robinGetHoldings(ticker=None):
    ROBINHOOD_USER = os.getenv("ROBINHOOD_USER")
    ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS")
    ROBINHOOD_MFA = os.getenv("ROBINHOOD_MFA")

    if not (ROBINHOOD_USER and ROBINHOOD_PASS and ROBINHOOD_MFA):
        print("No Robinhood credentials supplied, skipping")
        return None

    mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
    rh.login(ROBINHOOD_USER, ROBINHOOD_PASS, mfa_code=mfa, pickle_path="./tokens/")

    holdings_data = {}
    all_accounts = rh.account.load_account_profile(dataType="results")

    for account in all_accounts:
        account_number = account["account_number"]
        positions = rh.get_open_stock_positions(account_number=account_number)

        if not positions:
            continue

        formatted_positions = []
        for position in positions:
            symbol = rh.get_symbol_by_url(position['instrument'])
            quantity = float(position['quantity'])
            if ticker and symbol.upper() != ticker.upper():
                continue

            cost_basis = float(position['average_buy_price']) * quantity
            quote_data = rh.get_latest_price(symbol)
            current_price = float(quote_data[0]) if quote_data[0] else 0.0
            current_value = current_price * quantity

            formatted_positions.append({
                'symbol': symbol,
                'quantity': quantity,
                'cost_basis': cost_basis,
                'current_value': current_value
            })

        holdings_data[account_number] = formatted_positions

    return holdings_data


async def tradierTrade(side, qty, ticker, price):
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not TRADIER_ACCESS_TOKEN:
        print("Missing Tradier credentials, skipping")
        return None

    headers = {
        "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
        "Accept": "application/json",
    }

    client = httpx.Client()

    response = client.get("https://api.tradier.com/v1/user/profile", headers=headers)

    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return False

    profile_data = response.json()
    accounts = profile_data.get("profile", {}).get("account", [])
    if not accounts:
        print("No accounts found.")
        return False

    TRADIER_ACCOUNT_ID = [account["account_number"] for account in accounts]

    # Order placement
    order_type = "limit" if price else "market"
    price_data = {"price": f"{price}"} if price else {}

    for account_id in TRADIER_ACCOUNT_ID:
        response = client.post(
            f"https://api.tradier.com/v1/accounts/{account_id}/orders",
            data={
                "class": "equity",
                "symbol": ticker,
                "side": side,
                "quantity": qty,
                "type": order_type,
                "duration": "day",
                **price_data,
            },
            headers=headers
        )

        if response.status_code != 200:
            print(f"Error placing order on account {account_id}: {response.text}")
        else:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on Tradier account {account_id}")

    client.close()


async def tradierGetHoldings(ticker=None):
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not TRADIER_ACCESS_TOKEN:
        print("Missing Tradier credentials, skipping")
        return None

    headers = {
        "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
        "Accept": "application/json",
    }

    client = httpx.Client()

    response = client.get("https://api.tradier.com/v1/user/profile", headers=headers)

    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return None

    profile_data = response.json()
    accounts = profile_data.get("profile", {}).get("account", [])
    if not accounts:
        print("No accounts found.")
        return None

    holdings_data = {}

    # Get holdings for each account
    for account in accounts:
        account_id = account["account_number"]
        response = client.get(
            f"https://api.tradier.com/v1/accounts/{account_id}/positions",
            headers=headers,
        )

        if response.status_code != 200:
            print(f"Error getting positions for account {account_id}: {response.text}")
            continue

        positions = response.json().get("positions", {}).get("position", [])

        # Handle case where positions is None (no positions)
        if not positions:
            holdings_data[account_id] = []
            continue

        # Handle case where only one position is returned (comes as dict instead of list)
        if isinstance(positions, dict):
            positions = [positions]

        # If ticker is specified, filter for that ticker only
        if ticker:
            positions = [pos for pos in positions if pos.get("symbol") == ticker]

        # Get current quotes for all symbols
        symbols = [pos.get("symbol") for pos in positions]
        if symbols:
            quotes_response = client.get(
                "https://api.tradier.com/v1/markets/quotes",
                params={"symbols": ",".join(symbols)},
                headers=headers
            )
            quotes = quotes_response.json().get("quotes", {}).get("quote", [])
            if not isinstance(quotes, list):
                quotes = [quotes]
            quotes_dict = {quote.get("symbol"): quote.get("last") for quote in quotes}
        else:
            quotes_dict = {}

        holdings_data[account_id] = [{
            "symbol": pos.get("symbol"),
            "quantity": pos.get("quantity"),
            "cost_basis": pos.get("cost_basis"),
            "current_value": float(pos.get("quantity", 0)) * quotes_dict.get(pos.get("symbol"), 0)
        } for pos in positions]

    client.close()
    return holdings_data


async def tastyTrade(side, qty, ticker, price):
    TASTY_USER = os.getenv("TASTY_USER")
    TASTY_PASS = os.getenv("TASTY_PASS")

    if not (TASTY_USER or TASTY_PASS):
        print("No TastyTrade credentials supplied, skipping")
        return None

    session = Session(TASTY_USER, TASTY_PASS)
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

    for account in accounts:
        placed_order = account.place_order(session, order, dry_run=False)
        order_status = placed_order.order.status.value

        if order_status in ["Received", "Routed"]:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on TastyTrade {account.account_type_name} account {account.account_number}")


async def tastyGetHoldings(ticker=None):
    TASTY_USER = os.getenv("TASTY_USER")
    TASTY_PASS = os.getenv("TASTY_PASS")

    if not (TASTY_USER or TASTY_PASS):
        print("No TastyTrade credentials supplied, skipping")
        return None

    session = Session(TASTY_USER, TASTY_PASS)
    accounts = Account.get_accounts(session)

    holdings_data = {}
    for account in accounts:
        positions = account.get_positions(session)
        if not positions:
            continue

        formatted_positions = []
        for position in positions:
            # Skip if filtering by ticker and doesn't match
            if ticker and position.symbol != ticker:
                continue

            formatted_positions.append({
                "symbol": position.symbol,
                "quantity": float(position.quantity),
                "cost_basis": float(position.average_open_price),
                "current_value": float(position.close_price) * float(position.quantity)
            })

        holdings_data[account.account_number] = formatted_positions

    return holdings_data


async def publicTrade(side, qty, ticker, price):
    PUBLIC_USER = os.getenv("PUBLIC_USER")
    PUBLIC_PASS = os.getenv("PUBLIC_PASS")

    if not (PUBLIC_USER and PUBLIC_PASS):
        print("No Public credentials supplied, skipping")
        return None

    public = Public(path="./tokens/")
    public.login(username=PUBLIC_USER, password=PUBLIC_PASS, wait_for_2fa=True)

    order = public.place_order(
        symbol=ticker,
        quantity=qty,
        side=side,
        order_type='MARKET' if price is None else 'LIMIT',
        limit_price=None if price is None else price,
        time_in_force='DAY',
        tip=0,
    )

    if order["success"] is True:
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {ticker} on Public")


async def publicGetHoldings(ticker=None):
    PUBLIC_USER = os.getenv("PUBLIC_USER")
    PUBLIC_PASS = os.getenv("PUBLIC_PASS")

    if not (PUBLIC_USER and PUBLIC_PASS):
        print("No Public credentials supplied, skipping")
        return None

    try:
        public = Public(path="./tokens/")
        public.login(username=PUBLIC_USER, password=PUBLIC_PASS, wait_for_2fa=True)

        positions = public.get_positions()
        if not positions:
            return None

        holdings_data = {}
        formatted_positions = []

        for position in positions:
            # Skip if filtering by ticker and doesn't match
            if ticker and position['instrument']['symbol'] != ticker:
                continue

            # Extract relevant data with safe type conversion
            symbol = position['instrument']['symbol']
            quantity = float(position.get('quantity', 0) or 0)
            current_value = float(position.get('currentValue', 0) or 0)
            price_per_share = current_value / quantity if quantity else 0

            formatted_positions.append({
                "symbol": symbol,
                "quantity": quantity,
                "cost_basis": price_per_share,
                "current_value": current_value
            })

        # Store positions under a default account since Public typically has one account
        holdings_data["default"] = formatted_positions
        return holdings_data

    except Exception as e:
        print(f"Error getting Public holdings: {str(e)}")
        traceback.print_exc()
        return None


async def firstradeTrade(side, qty, ticker, price=None):
    FIRSTRADE_USER = os.getenv("FIRSTRADE_USER")
    FIRSTRADE_PASS = os.getenv("FIRSTRADE_PASS")
    FIRSTRADE_PIN = os.getenv("FIRSTRADE_PIN")

    ft_ss = ft_account.FTSession(
        username=FIRSTRADE_USER,
        password=FIRSTRADE_PASS,
        pin=FIRSTRADE_PIN,
        profile_path="./tokens/"
    )
    need_code = ft_ss.login()
    if need_code:
        code = input("Please enter the pin sent to your email/phone: ")
        ft_ss.login_two(code)

    ft_accounts = ft_account.FTAccountData(ft_ss)

    symbol_data = symbols.SymbolQuote(ft_ss, ft_accounts.account_numbers[0], ticker)
    
    adjusted_qty = qty
    sell_qty = 0
    
    if symbol_data.last < 1.00:
        if side == "buy":
            adjusted_qty = max(qty, 100)
            sell_qty = adjusted_qty - qty
        price_type = order.PriceType.LIMIT
        if price is None:
            if side == "buy":
                price = symbol_data.last + 0.01
            else:
                price = symbol_data.last - 0.01
    else:
        price_type = order.PriceType.MARKET if price is None else order.PriceType.LIMIT

    ft_order = order.Order(ft_ss)

    for account_number in ft_accounts.account_numbers:
        try:
            order_params = {
                "account": account_number,
                "symbol": ticker,
                "price_type": price_type,
                "order_type": order.OrderType.BUY if side == "buy" else order.OrderType.SELL,
                "quantity": adjusted_qty,
                "duration": order.Duration.DAY,
                "price": price,
                "dry_run": False,
            }

            order_conf = ft_order.place_order(**order_params)

            if order_conf.get("message") == "Normal":
                print(f"Order for {adjusted_qty} shares of {ticker} placed on Firstrade successfully.")
                print(f"Order ID: {order_conf.get('result').get('order_id')}.")
                
                if sell_qty > 0:
                    sell_params = {
                        **order_params,
                        "order_type": order.OrderType.SELL,
                        "quantity": sell_qty,
                        "price": symbol_data.last - 0.01,
                    }
                    
                    sell_conf = ft_order.place_order(**sell_params)
                    
                    if sell_conf.get("message") == "Normal":
                        print(f"Sell order for excess {sell_qty} shares placed successfully.")
                        print(f"Sell Order ID: {sell_conf.get('result').get('order_id')}.")
                    else:
                        print("Failed to place sell order for excess shares on Firstrade.")
                        print(sell_conf)
            else:
                print(f"Failed to place order for {ticker} on Firstrade.")
                print(order_conf)
        except Exception as e:
            print(f"An error occurred while placing order for {ticker} on Firstrade: {e}")


async def firstradeGetHoldings(ticker=None):
    FIRSTRADE_USER = os.getenv("FIRSTRADE_USER")
    FIRSTRADE_PASS = os.getenv("FIRSTRADE_PASS")
    FIRSTRADE_PIN = os.getenv("FIRSTRADE_PIN")

    if not (FIRSTRADE_USER and FIRSTRADE_PASS and FIRSTRADE_PIN):
        print("No Firstrade credentials supplied, skipping")
        return None

    try:
        ft_ss = ft_account.FTSession(
            username=FIRSTRADE_USER,
            password=FIRSTRADE_PASS,
            pin=FIRSTRADE_PIN,
            profile_path="./tokens/"
        )
        
        need_code = ft_ss.login()
        if need_code:
            code = input("Please enter the pin sent to your email/phone: ")
            ft_ss.login_two(code)

        ft_accounts = ft_account.FTAccountData(ft_ss)
        holdings_data = {}

        for account_number in ft_accounts.account_numbers:
            positions = ft_accounts.get_positions(account_number)
            if not positions:
                continue

            formatted_positions = []
            for position in positions.get("items", []):
                symbol = position.get("symbol")
                quantity = float(position.get("quantity", 0))
                cost_basis = float(position.get("cost", 0))
                current_value = float(position.get("market_value", 0))

                if ticker and symbol.upper() != ticker.upper():
                    continue

                formatted_positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "current_value": current_value
                })

            if formatted_positions:
                holdings_data[account_number] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Firstrade holdings: {str(e)}")
        traceback.print_exc()
        return None


async def fennelTrade(side, qty, ticker, price):
    FENNEL_EMAIL = os.getenv("FENNEL_EMAIL")

    if not FENNEL_EMAIL:
        print("No Fennel credentials supplied, skipping")
        return None

    fennel = Fennel(path="./tokens/")
    fennel.login(email=FENNEL_EMAIL, wait_for_code=True)

    account_ids = fennel.get_account_ids()
    for account_id in account_ids:
        order = fennel.place_order(
            account_id=account_id,
            ticker=ticker,
            quantity=qty,
            side=side,
            price="market",
        )

        if order.get('data', {}).get('createOrder') == 'pending':
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on Fennel account {account_id}")
        else:
            print(f"Failed to place order for {ticker} on Fennel account {account_id}")


async def fennelGetHoldings(ticker=None):
    FENNEL_EMAIL = os.getenv("FENNEL_EMAIL")

    if not FENNEL_EMAIL:
        print("No Fennel credentials supplied, skipping")
        return None

    try:
        fennel = Fennel(path="./tokens/")
        fennel.login(email=FENNEL_EMAIL, wait_for_code=True)

        account_ids = fennel.get_account_ids()
        holdings_data = {}

        for account_id in account_ids:
            portfolio = fennel.get_stock_holdings(account_id)
            formatted_positions = []

            for position in portfolio:
                symbol = position['security']['ticker']
                quantity = float(position['investment']['ownedShares'])
                cost_basis = float(position['security']['currentStockPrice']) * quantity
                current_value = float(position['investment']['marketValue'])

                if ticker and symbol.upper() != ticker.upper():
                    continue

                formatted_positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "current_value": current_value
                })

            holdings_data[account_id] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Fennel holdings: {str(e)}")
        traceback.print_exc()
        return None


async def schwabTrade(side, qty, ticker, price):
    SCHWAB_API_KEY = os.getenv("SCHWAB_API_KEY")
    SCHWAB_API_SECRET = os.getenv("SCHWAB_API_SECRET")
    SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL")
    SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH")

    c = auth.easy_client(
        SCHWAB_API_KEY,
        SCHWAB_API_SECRET,
        SCHWAB_CALLBACK_URL,
        SCHWAB_TOKEN_PATH,
        interactive=False
    )

    accounts = c.get_account_numbers()

    order_types = {
        ("buy", True): equity_buy_limit,
        ("buy", False): equity_buy_market,
        ("sell", True): equity_sell_limit,
        ("sell", False): equity_sell_market,
    }

    order_function = order_types.get((side.lower(), bool(price)))
    if not order_function:
        raise ValueError(f"Invalid combination of side: {side} and price: {price}")

    for account in accounts.json():
        account_hash = account["hashValue"]
        order = c.place_order(
            account_hash,
            (
                order_function(ticker, qty, price)
                if price
                else order_function(ticker, qty)
            ),
        )

        if order.status_code == 201:
            print(f"Order placed for {qty} shares of {ticker} on Schwab account {account['accountNumber']}")
        else:
            print(f"Error placing order on Schwab account {account['accountNumber']}: {order.json()}")


async def schwabGetHoldings(ticker=None):
    SCHWAB_API_KEY = os.getenv("SCHWAB_API_KEY")
    SCHWAB_API_SECRET = os.getenv("SCHWAB_API_SECRET")
    SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL")
    SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH")

    if not (SCHWAB_API_KEY and SCHWAB_API_SECRET and SCHWAB_CALLBACK_URL and SCHWAB_TOKEN_PATH):
        print("No Schwab credentials supplied, skipping")
        return None

    c = auth.easy_client(
        SCHWAB_API_KEY,
        SCHWAB_API_SECRET,
        SCHWAB_CALLBACK_URL,
        SCHWAB_TOKEN_PATH,
        interactive=False,
    )

    accounts_response = c.get_account_numbers()
    if accounts_response.status_code != 200:
        print(f"Error getting Schwab accounts: {accounts_response.text}")
        return None

    accounts = accounts_response.json()
    holdings_data = {}

    for account in accounts:
        account_number = account['accountNumber']
        account_hash = account['hashValue']
        positions_response = c.get_account(
            account_hash,
            fields=c.Account.Fields.POSITIONS
        )

        if positions_response.status_code != 200:
            print(f"Error getting positions for account {account_number}: {positions_response.text}")
            continue

        # Update parsing logic based on the data structure
        positions_data = positions_response.json()
        securities_account = positions_data.get('securitiesAccount', {})
        positions = securities_account.get('positions', [])

        # Handle case where positions is a dict (single position)
        if isinstance(positions, dict):
            positions = [positions]

        formatted_positions = []
        for position in positions:
            instrument = position.get('instrument', {})
            symbol = instrument.get('symbol')
            quantity = float(position.get('longQuantity', 0))
            average_price = float(position.get('averagePrice', 0))
            market_value = float(position.get('marketValue', 0))

            if not symbol:
                continue

            if ticker and symbol.upper() != ticker.upper():
                continue

            formatted_positions.append({
                'symbol': symbol,
                'quantity': quantity,
                'cost_basis': average_price * quantity,
                'current_value': market_value
            })

        holdings_data[account_number] = formatted_positions

    return holdings_data if holdings_data else None


async def bbaeTrade(side, qty, ticker, price=None):
    BBAE_USER = os.getenv("BBAE_USER")
    BBAE_PASS = os.getenv("BBAE_PASS")

    if not (BBAE_USER and BBAE_PASS):
        print("No BBAE credentials supplied, skipping")
        return None

    bbae = BBAEAPI(BBAE_USER, BBAE_PASS, creds_path="./tokens/")
    
    if not await _login_broker(bbae, "BBAE"):
        return None

    account_info = bbae.get_account_info()
    account_number = account_info.get("Data").get('accountNumber')

    if not account_number:
        print("Failed to retrieve account number from BBAE.")
        return None
    
    if side == 'buy':
        response = bbae.execute_buy(ticker, qty, account_number, dry_run=False)
    elif side == 'sell':
        holdings_response = bbae.check_stock_holdings(ticker, account_number)
        available_qty = holdings_response.get("Data").get('enableAmount', 0)

        if int(available_qty) < qty:
            print(f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}")
            return None

        response = bbae.execute_sell(ticker, qty, account_number, price, dry_run=False)
    else:
        print(f"Invalid trade side: {side}")
        return None

    if response.get("Outcome") == "Success":
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {qty} shares of {ticker} on BBAE.")
    else:
        print(f"Failed to {side} {ticker}: {response.get('Message')}")


async def dspacTrade(side, qty, ticker, price=None):
    DSPAC_USER = os.getenv("DSPAC_USER")
    DSPAC_PASS = os.getenv("DSPAC_PASS")

    if not (DSPAC_USER and DSPAC_PASS):
        print("No DSPAC credentials supplied, skipping")
        return None

    dspac = DSPACAPI(DSPAC_USER, DSPAC_PASS, creds_path="./tokens/")
    
    if not await _login_broker(dspac, "DSPAC"):
        return None

    account_info = dspac.get_account_info()
    account_number = account_info.get("Data").get('accountNumber')

    if not account_number:
        print("Failed to retrieve account number from DSPAC.")
        return None
    
    if side == 'buy':
        response = dspac.execute_buy(ticker, qty, account_number, dry_run=False)
    elif side == 'sell':
        holdings_response = dspac.check_stock_holdings(ticker, account_number)
        available_qty = holdings_response.get("Data").get('enableAmount', 0)

        if int(available_qty) < qty:
            print(f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}")
            return None

        response = dspac.execute_sell(ticker, qty, account_number, price, dry_run=False)
    else:
        print(f"Invalid trade side: {side}")
        return None

    if response.get("Outcome") == "Success":
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {qty} shares of {ticker} on DSPAC.")
    else:
        print(f"Failed to {side} {ticker}: {response.get('Message')}")


async def bbaeGetHoldings(ticker=None):
    BBAE_USER = os.getenv("BBAE_USER")
    BBAE_PASS = os.getenv("BBAE_PASS")

    if not (BBAE_USER and BBAE_PASS):
        print("No BBAE credentials supplied, skipping")
        return None

    bbae = BBAEAPI(BBAE_USER, BBAE_PASS, creds_path="./tokens/")
    
    if not await _login_broker(bbae, "BBAE"):
        return None
        
    return await _get_broker_holdings(bbae, "BBAE", ticker)

async def dspacGetHoldings(ticker=None):
    DSPAC_USER = os.getenv("DSPAC_USER")
    DSPAC_PASS = os.getenv("DSPAC_PASS")

    if not (DSPAC_USER and DSPAC_PASS):
        print("No DSPAC credentials supplied, skipping")
        return None

    dspac = DSPACAPI(DSPAC_USER, DSPAC_PASS, creds_path="./tokens/")
    
    if not await _login_broker(dspac, "DSPAC"):
        return None
        
    return await _get_broker_holdings(dspac, "DSPAC", ticker)

