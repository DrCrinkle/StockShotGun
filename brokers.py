import os
import httpx
import asyncio
import pyotp
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


async def robinTrade(side, qty, ticker, price):
    ROBINHOOD_USER = os.getenv("ROBINHOOD_USER")
    ROBINHOOD_PASS = os.getenv("ROBINHOOD_PASS")
    ROBINHOOD_MFA  = os.getenv("ROBINHOOD_MFA")

    if not (ROBINHOOD_USER and ROBINHOOD_PASS and ROBINHOOD_MFA):
        print("No Robinhood credentials supplied, skipping")
        return None

    mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
    await asyncio.to_thread(rh.login, ROBINHOOD_USER, ROBINHOOD_PASS, mfa_code=mfa)

    all_accounts = await asyncio.to_thread(rh.account.load_account_profile, dataType="results")

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

        await asyncio.to_thread(order_function, **order_args)
        action_str = "Bought" if side == "buy" else "Sold"
        
        print(f"{action_str} {ticker} on Robinhood {brokerage_account_type} account {account_number}")


async def tradierTrade(side, qty, ticker, price):
    TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

    if not TRADIER_ACCESS_TOKEN:
        print("Missing Tradier credentials, skipping")
        return None

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.tradier.com/v1/user/profile",
            headers={
                "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
                "Accept": "application/json",
            },
        )

        if response.status_code != 200:
            print(f"Error: {response.status} - {await response.text()}")
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
            response = await client.post(
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
                headers={
                    "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
                    "Accept": "application/json",
                },
            )

            if response.status_code != 200:
                print(
                    f"Error placing order on account {account_id}: {await response.text()}"
                )
            else:
                action_str = "Bought" if side == "buy" else "Sold"
                print(f"{action_str} {ticker} on Tradier account {account_id}")


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

    for acc in accounts:
        placed_order = acc.place_order(session, order, dry_run = False)
        order_status = placed_order.order.status.value

        if order_status in ["Received", "Routed"]:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on TastyTrade {acc.account_type_name} account {acc.account_number}")

async def publicTrade(side, qty, ticker, price):
    PUBLIC_USER = os.getenv("PUBLIC_USER")
    PUBLIC_PASS = os.getenv("PUBLIC_PASS")

    if not (PUBLIC_USER and PUBLIC_PASS):
        print("No Public credentials supplied, skipping")
        return None

    public = Public(path="./tokens/")
    await asyncio.to_thread(public.login, username=PUBLIC_USER, password=PUBLIC_PASS, wait_for_2fa=True)

    order = await asyncio.to_thread(
        public.place_order,
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


async def firstradeTrade(side, qty, ticker):
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
    
    # Firstrade does not allow market orders for stocks under $1.00
    symbol_data = symbols.SymbolQuote(ft_ss, ft_accounts.account_numbers[0], ticker)
    if symbol_data.last < 1.00:
        price_type = order.PriceType.LIMIT
        if side == "buy":
            price = symbol_data.bid + 0.01
        else:
            price = symbol_data.ask - 0.01
    else:
        price_type = order.PriceType.MARKET
        price = None

    ft_order = order.Order(ft_ss)

    for account_number in ft_accounts.account_numbers:
        try:
            order_conf = await asyncio.to_thread(
                ft_order.place_order,
                account_number,
                symbol=ticker,
                price_type=price_type,
                order_type=order.OrderType.BUY if side == "buy" else order.OrderType.SELL,
                quantity=qty,
                duration=order.Duration.DAY,
                price=price,
                dry_run=False,
            )

            if order_conf.get("message") == "Normal":
                print(f"Order for {ticker} placed on Firstrade successfully.")
                print(f"Order ID: {order_conf.get("result").get('order_id')}.")
            else:
                print(f"Failed to place order for {ticker} on Firstrade.")
                print(order_conf)
        except Exception as e:
            print(f"An error occurred while placing order for {ticker} on Firstrade: {e}")


async def fennelTrade(side, qty, ticker, price):
    FENNEL_EMAIL = os.getenv("FENNEL_EMAIL")

    if not FENNEL_EMAIL:
        print("No Fennel credentials supplied, skipping")
        return None

    fennel = Fennel(path="./tokens/")
    await asyncio.to_thread(fennel.login, email=FENNEL_EMAIL, wait_for_code=True)

    account_ids = await asyncio.to_thread(fennel.get_account_ids)
    for account_id in account_ids:
        order = await asyncio.to_thread(
            fennel.place_order,
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


async def bbaeTrade(side, qty, ticker, price=None):
    BBAE_USER = os.getenv("BBAE_USER")
    BBAE_PASS = os.getenv("BBAE_PASS")

    if not (BBAE_USER and BBAE_PASS):
        print("No BBAE credentials supplied, skipping")
        return None

    bbae = BBAEAPI(BBAE_USER, BBAE_PASS, creds_path="./tokens/")

    await asyncio.to_thread(bbae.make_initial_request)
    login_ticket = await asyncio.to_thread(bbae.generate_login_ticket_email)
    if login_ticket.get("Data") is None:
        raise Exception("Invalid response from generating login ticket")
    if login_ticket.get("Data").get("needSmsVerifyCode", False):
        if login_ticket.get("Data").get("needCaptchaCode", False):
            captcha_image = bbae.request_captcha()
            captcha_image.save("./BBAEcaptcha.png", format="PNG")
            captcha_input = input(
                "CAPTCHA image saved to ./BBAEcaptcha.png. Please open it and type in the code: "
            )
            bbae.request_email_code(captcha_input=captcha_input)
            otp_code = input("Enter BBAE security code: ")
        else:
            bbae.request_email_code()
            otp_code = input("Enter BBAE security code: ")
        
        login_ticket = await asyncio.to_thread(bbae.generate_login_ticket_email, otp_code)        
    
    login_response = await asyncio.to_thread(bbae.login_with_ticket, login_ticket.get("Data").get("ticket"))
    if login_response.get("Outcome") != "Success":
        raise Exception(f"Login failed. Response: {login_response}")

    account_info = await asyncio.to_thread(bbae.get_account_info)
    account_number = account_info.get("Data").get('accountNumber')

    if not account_number:
        print("Failed to retrieve account number from BBAE.")
        return None
    
    if side == 'buy':
        response = await asyncio.to_thread(bbae.execute_buy, ticker, qty, account_number, dry_run=False)
    elif side == 'sell':
        holdings_response = await asyncio.to_thread(bbae.check_stock_holdings, ticker, account_number)
        available_qty = holdings_response.get("Data").get('enableAmount', 0)

        if int(available_qty) < qty:
            print(f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}")
            return None
        
        response = await asyncio.to_thread(bbae.execute_sell, ticker, qty, account_number, price, dry_run=False)
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

    await asyncio.to_thread(dspac.make_initial_request)
    login_ticket = await asyncio.to_thread(dspac.generate_login_ticket_email)
    if login_ticket.get("Data") is None:
        raise Exception("Invalid response from generating login ticket")
    if login_ticket.get("Data").get("needSmsVerifyCode", False):
        if login_ticket.get("Data").get("needCaptchaCode", False):
            captcha_image = dspac.request_captcha()
            captcha_image.save("./DSPACcaptcha.png", format="PNG")
            captcha_input = input(
                "CAPTCHA image saved to ./DSPACcaptcha.png. Please open it and type in the code: "
            )
            dspac.request_email_code(captcha_input=captcha_input)
            otp_code = input("Enter DSPAC security code: ")
        else:
            dspac.request_email_code()
            otp_code = input("Enter DSPAC security code: ")
        
        login_ticket = await asyncio.to_thread(dspac.generate_login_ticket_email, otp_code)        
    
    login_response = await asyncio.to_thread(dspac.login_with_ticket, login_ticket.get("Data").get("ticket"))
    if login_response.get("Outcome") != "Success":
        raise Exception(f"Login failed. Response: {login_response}")

    account_info = await asyncio.to_thread(dspac.get_account_info)
    account_number = account_info.get("Data").get('accountNumber')

    if not account_number:
        print("Failed to retrieve account number from DSPAC.")
        return None
    
    if side == 'buy':
        response = await asyncio.to_thread(dspac.execute_buy, ticker, qty, account_number, dry_run=False)
    elif side == 'sell':
        holdings_response = await asyncio.to_thread(dspac.check_stock_holdings, ticker, account_number)
        available_qty = holdings_response.get("Data").get('enableAmount', 0)

        if int(available_qty) < qty:
            print(f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}")
            return None
        
        response = await asyncio.to_thread(dspac.execute_sell, ticker, qty, account_number, price, dry_run=False)
    else:
        print(f"Invalid trade side: {side}")
        return None

    if response.get("Outcome") == "Success":
        action_str = "Bought" if side == "buy" else "Sold"
        print(f"{action_str} {qty} shares of {ticker} on DSPAC.")
    else:
        print(f"Failed to {side} {ticker}: {response.get('Message')}")
