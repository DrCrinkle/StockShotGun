"""Schwab broker integration."""

import asyncio
import os
from schwab import auth
from schwab.orders.equities import (
    equity_buy_limit,
    equity_buy_market,
    equity_sell_limit,
    equity_sell_market,
)


async def schwabTrade(side, qty, ticker, price):
    """Execute a trade on Schwab."""
    from .base import rate_limiter
    await rate_limiter.wait_if_needed("Schwab")

    from .session_manager import session_manager
    c = await session_manager.get_session("Schwab")
    if not c:
        print("No Schwab credentials supplied, skipping")
        return None

    accounts = await asyncio.to_thread(c.get_account_numbers)

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
        order = await asyncio.to_thread(
            c.place_order,
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
    """Get holdings from Schwab."""
    from .base import rate_limiter
    await rate_limiter.wait_if_needed("Schwab")

    from .session_manager import session_manager
    c = await session_manager.get_session("Schwab")
    if not c:
        print("No Schwab credentials supplied, skipping")
        return None

    accounts_response = await asyncio.to_thread(c.get_account_numbers)
    if accounts_response.status_code != 200:
        print(f"Error getting Schwab accounts: {accounts_response.text}")
        return None

    accounts = accounts_response.json()
    holdings_data = {}

    for account in accounts:
        account_number = account['accountNumber']
        account_hash = account['hashValue']
        positions_response = await asyncio.to_thread(
            c.get_account,
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


async def get_schwab_session(session_manager):
    """Get or create Schwab session."""
    if "schwab" not in session_manager._initialized:
        SCHWAB_API_KEY = os.getenv("SCHWAB_API_KEY")
        SCHWAB_API_SECRET = os.getenv("SCHWAB_API_SECRET")
        SCHWAB_CALLBACK_URL = os.getenv("SCHWAB_CALLBACK_URL")
        SCHWAB_TOKEN_PATH = os.getenv("SCHWAB_TOKEN_PATH")

        if not (SCHWAB_API_KEY and SCHWAB_API_SECRET and SCHWAB_CALLBACK_URL and SCHWAB_TOKEN_PATH):
            session_manager.sessions["schwab"] = None
            session_manager._initialized.add("schwab")
            return None

        try:
            client = await asyncio.to_thread(
                auth.easy_client,
                SCHWAB_API_KEY,
                SCHWAB_API_SECRET,
                SCHWAB_CALLBACK_URL,
                SCHWAB_TOKEN_PATH,
                interactive=False
            )
            session_manager.sessions["schwab"] = client
            print("✓ Schwab session initialized")
        except Exception as e:
            print(f"✗ Failed to initialize Schwab session: {e}")
            session_manager.sessions["schwab"] = None

        session_manager._initialized.add("schwab")

    return session_manager.sessions.get("schwab")
