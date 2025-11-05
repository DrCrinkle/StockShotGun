"""Robinhood broker integration."""

import asyncio
import pyotp
import robin_stocks.robinhood as rh
from .base import retry_operation, rate_limiter


async def robinTrade(side, qty, ticker, price):
    """Execute a trade on Robinhood."""
    await rate_limiter.wait_if_needed("Robinhood")

    from .session_manager import session_manager
    session = await session_manager.get_session("Robinhood")
    if not session:
        print("No Robinhood credentials supplied, skipping")
        return None

    all_accounts = await asyncio.to_thread(
        rh.account.load_account_profile, dataType="results"
    )

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


async def robinGetHoldings(ticker=None):
    """Get holdings from Robinhood."""
    await rate_limiter.wait_if_needed("Robinhood")

    from .session_manager import session_manager
    session = await session_manager.get_session("Robinhood")
    if not session:
        print("No Robinhood credentials supplied, skipping")
        return None

    holdings_data = {}
    all_accounts = await asyncio.to_thread(
        rh.account.load_account_profile, dataType="results"
    )

    for account in all_accounts:
        account_number = account["account_number"]
        positions = await asyncio.to_thread(
            rh.get_open_stock_positions, account_number=account_number
        )

        if not positions:
            continue

        formatted_positions = []
        for position in positions:
            symbol = await asyncio.to_thread(
                rh.get_symbol_by_url, position['instrument']
            )
            quantity = float(position['quantity'])
            if ticker and symbol.upper() != ticker.upper():
                continue

            cost_basis = float(position['average_buy_price']) * quantity
            quote_data = await asyncio.to_thread(rh.get_latest_price, symbol)
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


async def get_robinhood_session(session_manager):
    """Get or create Robinhood session."""
    async with session_manager._get_session_lock("robinhood"):
        if "robinhood" not in session_manager._initialized:
            ROBINHOOD_USER = session_manager._get_env("ROBINHOOD_USER")
            ROBINHOOD_PASS = session_manager._get_env("ROBINHOOD_PASS")
            ROBINHOOD_MFA = session_manager._get_env("ROBINHOOD_MFA")

            if not (ROBINHOOD_USER and ROBINHOOD_PASS and ROBINHOOD_MFA):
                session_manager.sessions["robinhood"] = None
                session_manager._initialized.add("robinhood")
                return None

            async def _robinhood_login():
                mfa = pyotp.TOTP(ROBINHOOD_MFA).now()
                await asyncio.to_thread(
                    rh.login, ROBINHOOD_USER, ROBINHOOD_PASS,
                    mfa_code=mfa, pickle_path="./tokens/"
                )
                return True

            try:
                await retry_operation(_robinhood_login)
                session_manager.sessions["robinhood"] = True  # RH uses global state
                print("✓ Robinhood session initialized")
            except Exception as e:
                print(f"✗ Failed to initialize Robinhood session: {e}")
                session_manager.sessions["robinhood"] = None

            session_manager._initialized.add("robinhood")

        return session_manager.sessions.get("robinhood")
