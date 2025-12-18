"""TastyTrade broker integration."""

import asyncio
import os
import traceback
from decimal import Decimal
from tastytrade import Session, Account
from tastytrade.instruments import Equity
from tastytrade.order import (
    NewOrder,
    OrderTimeInForce,
    OrderType,
    OrderAction,
)
from .base import rate_limiter, retry_operation


async def tastyTrade(side, qty, ticker, price):
    """Execute a trade on TastyTrade.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("TastyTrade")

    from .session_manager import session_manager
    session = await session_manager.get_session("TastyTrade")
    if not session:
        print("No TastyTrade credentials supplied, skipping")
        return None

    success_count = 0
    failure_count = 0

    try:
        accounts = await asyncio.to_thread(Account.get, session)
        symbol = await asyncio.to_thread(Equity.get, session, ticker)
        action = OrderAction.BUY_TO_OPEN if side == "buy" else OrderAction.SELL_TO_CLOSE

        # Build the order
        leg = symbol.build_leg(Decimal(qty), action)
        order_type = OrderType.LIMIT if price else OrderType.MARKET
        price_value = Decimal(f'-{price}') if price and side == "buy" else Decimal(f'{price}') if price else None
        order = NewOrder(
            time_in_force=OrderTimeInForce.DAY,
            order_type=order_type,
            legs=[leg],
            price=price_value
        )

        for account in accounts:
            try:
                placed_order = await account.a_place_order(session, order, dry_run=False)
                order_status = placed_order.order.status.value

                if order_status in ["Received", "Routed"]:
                    action_str = "Bought" if side == "buy" else "Sold"
                    print(f"{action_str} {ticker} on TastyTrade {account.account_type_name} account {account.account_number}")
                    success_count += 1
                else:
                    print(f"Order for {ticker} on TastyTrade account {account.account_number} has status: {order_status}")
                    failure_count += 1
            except Exception as e:
                print(f"Error placing order for {ticker} on TastyTrade account {account.account_number}: {str(e)}")
                failure_count += 1
    except Exception as e:
        print(f"Error trading {ticker} on TastyTrade: {str(e)}")
        traceback.print_exc()
        return False

    return success_count > 0


async def tastyGetHoldings(ticker=None):
    """Get holdings from TastyTrade."""
    await rate_limiter.wait_if_needed("TastyTrade")

    from .session_manager import session_manager
    session = await session_manager.get_session("TastyTrade")
    if not session:
        print("No TastyTrade credentials supplied, skipping")
        return None

    accounts = await asyncio.to_thread(Account.get, session)

    holdings_data = {}
    for account in accounts:
        positions = await asyncio.to_thread(account.get_positions, session)
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


async def get_tastytrade_session(session_manager):
    """Get or create TastyTrade session."""
    if "tastytrade" not in session_manager._initialized:
        TASTY_USER = os.getenv("TASTY_USER")
        TASTY_PASS = os.getenv("TASTY_PASS")

        if not (TASTY_USER and TASTY_PASS):
            session_manager.sessions["tastytrade"] = None
            session_manager._initialized.add("tastytrade")
            return None

        async def _create_tastytrade_session():
            return await asyncio.to_thread(Session, TASTY_USER, TASTY_PASS)

        try:
            session = await retry_operation(_create_tastytrade_session)
            session_manager.sessions["tastytrade"] = session
            print("✓ TastyTrade session initialized")
        except Exception as e:
            print(f"✗ Failed to initialize TastyTrade session: {e}")
            session_manager.sessions["tastytrade"] = None

        session_manager._initialized.add("tastytrade")

    return session_manager.sessions.get("tastytrade")
