"""Firstrade broker integration."""

import asyncio
import os
import traceback
from firstrade import account as ft_account, order, symbols
from brokers.base import retry_operation


async def firstradeTrade(side, qty, ticker, price):
    """Execute a trade on Firstrade.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials (broker skipped)
    """
    from brokers.base import rate_limiter

    await rate_limiter.wait_if_needed("Firstrade")

    from brokers.session_manager import session_manager

    ft_ss = await session_manager.get_session("Firstrade")
    if not ft_ss:
        print("No Firstrade credentials supplied, skipping")
        return None

    ft_accounts = await asyncio.to_thread(ft_account.FTAccountData, ft_ss)

    symbol_data = await asyncio.to_thread(
        symbols.SymbolQuote, ft_ss, ft_accounts.account_numbers[0], ticker
    )

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

    ft_order = await asyncio.to_thread(order.Order, ft_ss)

    success_count = 0
    failure_count = 0

    for account_number in ft_accounts.account_numbers:
        try:
            order_params = {
                "account": account_number,
                "symbol": ticker,
                "price_type": price_type,
                "order_type": order.OrderType.BUY
                if side == "buy"
                else order.OrderType.SELL,
                "quantity": adjusted_qty,
                "duration": order.Duration.DAY,
                "price": price,
                "dry_run": False,
            }

            order_conf = await asyncio.to_thread(ft_order.place_order, **order_params)

            if order_conf.get("message") == "Normal":
                print(
                    f"Order for {adjusted_qty} shares of {ticker} placed on Firstrade account {account_number} successfully."
                )
                print(f"Order ID: {order_conf.get('result').get('order_id')}.")
                success_count += 1

                if sell_qty > 0:
                    sell_params = {
                        **order_params,
                        "order_type": order.OrderType.SELL,
                        "quantity": sell_qty,
                        "price": symbol_data.last - 0.01,
                    }

                    sell_conf = await asyncio.to_thread(
                        ft_order.place_order, **sell_params
                    )

                    if sell_conf.get("message") == "Normal":
                        print(
                            f"Sell order for excess {sell_qty} shares on Firstrade account {account_number} placed successfully."
                        )
                        print(
                            f"Sell Order ID: {sell_conf.get('result').get('order_id')}."
                        )
                    else:
                        print(
                            f"Failed to place sell order for excess shares on Firstrade account {account_number}."
                        )
                        print(sell_conf)
            else:
                print(f"Failed to place order for {ticker} on Firstrade account {account_number}.")
                print(order_conf)
                failure_count += 1
        except Exception as e:
            print(
                f"An error occurred while placing order for {ticker} on Firstrade account {account_number}: {e}"
            )
            failure_count += 1

    # Return True if at least one account succeeded
    return success_count > 0


async def firstradeValidate(side, qty, ticker, price):
    """Validate order via Firstrade dry-run.

    Returns:
        (True, ""): Order is valid
        (False, reason): Order would fail
        (None, ""): No credentials
    """
    from brokers.base import rate_limiter

    await rate_limiter.wait_if_needed("Firstrade")

    from brokers.session_manager import session_manager

    ft_ss = await session_manager.get_session("Firstrade")
    if not ft_ss:
        return (None, "")

    try:
        ft_accounts = await asyncio.to_thread(ft_account.FTAccountData, ft_ss)
        symbol_data = await asyncio.to_thread(
            symbols.SymbolQuote, ft_ss, ft_accounts.account_numbers[0], ticker
        )

        adjusted_qty = qty
        if symbol_data.last < 1.00 and side == "buy":
            adjusted_qty = max(qty, 100)
            price_type = order.PriceType.LIMIT
            if price is None:
                price = symbol_data.last + 0.01
        else:
            price_type = order.PriceType.MARKET if price is None else order.PriceType.LIMIT

        ft_order = await asyncio.to_thread(order.Order, ft_ss)
        order_conf = await asyncio.to_thread(
            ft_order.place_order,
            account=ft_accounts.account_numbers[0],
            symbol=ticker,
            price_type=price_type,
            order_type=order.OrderType.BUY if side == "buy" else order.OrderType.SELL,
            quantity=adjusted_qty,
            duration=order.Duration.DAY,
            price=price,
            dry_run=True,
        )

        # Check for error responses
        if isinstance(order_conf, dict):
            if order_conf.get("error"):
                msg = order_conf["error"]
                if isinstance(msg, dict):
                    msg = msg.get("message", str(msg))
                return (False, str(msg)[:100])
            if order_conf.get("statusCode") and order_conf["statusCode"] != 200:
                msg = order_conf.get("message", "Validation failed")
                return (False, str(msg)[:100])

        return (True, "")
    except Exception as e:
        return (False, str(e).split("\n")[0][:100])


async def firstradeGetHoldings(ticker=None):
    """Get holdings from Firstrade."""
    from brokers.base import rate_limiter

    await rate_limiter.wait_if_needed("Firstrade")

    from brokers.session_manager import session_manager

    ft_ss = await session_manager.get_session("Firstrade")
    if not ft_ss:
        print("No Firstrade credentials supplied, skipping")
        return None

    try:
        ft_accounts = await asyncio.to_thread(ft_account.FTAccountData, ft_ss)
        holdings_data = {}

        for account_number in ft_accounts.account_numbers:
            positions = await asyncio.to_thread(
                ft_accounts.get_positions, account_number
            )
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

                formatted_positions.append(
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "current_value": current_value,
                    }
                )

            if formatted_positions:
                holdings_data[account_number] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Firstrade holdings: {str(e)}")
        traceback.print_exc()
        return None


async def get_firstrade_session(session_manager):
    """Get or create Firstrade session."""
    if "firstrade" not in session_manager._initialized:
        FIRSTRADE_USER = os.getenv("FIRSTRADE_USER")
        FIRSTRADE_PASS = os.getenv("FIRSTRADE_PASS")
        FIRSTRADE_MFA = os.getenv("FIRSTRADE_MFA")

        if not (FIRSTRADE_USER and FIRSTRADE_PASS and FIRSTRADE_MFA):
            session_manager.sessions["firstrade"] = None
            session_manager._initialized.add("firstrade")
            return None

        async def _create_firstrade_session():
            """Create Firstrade session with retry support."""
            return await asyncio.to_thread(
                ft_account.FTSession,
                username=FIRSTRADE_USER,
                password=FIRSTRADE_PASS,
                mfa_secret=FIRSTRADE_MFA,
                profile_path="./tokens/",
            )

        try:
            ft_ss = await retry_operation(_create_firstrade_session)
            need_code = await asyncio.to_thread(ft_ss.login)
            if need_code:
                code = input("Please enter the pin sent to your email/phone: ")
                await asyncio.to_thread(ft_ss.login_two, code)

            session_manager.sessions["firstrade"] = ft_ss
            print("✓ Firstrade session initialized")
        except Exception as e:
            print(f"✗ Failed to initialize Firstrade session: {e}")
            session_manager.sessions["firstrade"] = None

        session_manager._initialized.add("firstrade")

    return session_manager.sessions.get("firstrade")
