"""BBAE broker integration."""

import asyncio
import os
import traceback
from bbae_invest_api import BBAEAPI
from brokers.base import _login_broker, _get_broker_holdings, rate_limiter


async def bbaeTrade(side, qty, ticker, price):
    """Execute a trade on BBAE.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("BBAE")

    from session_manager import session_manager

    bbae = await session_manager.get_session("BBAE")
    if not bbae:
        print("No BBAE credentials supplied, skipping")
        return None

    success_count = 0
    failure_count = 0

    try:
        account_info = await asyncio.to_thread(bbae.get_account_info)
        account_number = account_info.get("Data").get("accountNumber")

        if not account_number:
            print("Failed to retrieve account number from BBAE.")
            return False

        if side == "buy":
            response = await asyncio.to_thread(
                bbae.execute_buy, ticker, qty, account_number, dry_run=False
            )
        elif side == "sell":
            holdings_response = await asyncio.to_thread(
                bbae.check_stock_holdings, ticker, account_number
            )
            available_qty = holdings_response.get("Data").get("enableAmount", 0)

            if int(available_qty) < qty:
                print(
                    f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}"
                )
                return False

            response = await asyncio.to_thread(
                bbae.execute_sell, ticker, qty, account_number, price, dry_run=False
            )
        else:
            print(f"Invalid trade side: {side}")
            return False

        if response.get("Outcome") == "Success":
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {qty} shares of {ticker} on BBAE.")
            success_count += 1
        else:
            print(f"Failed to {side} {ticker}: {response.get('Message')}")
            failure_count += 1
    except Exception as e:
        print(f"Error trading {ticker} on BBAE: {str(e)}")
        traceback.print_exc()
        failure_count += 1

    return success_count > 0


async def bbaeGetHoldings(ticker=None):
    """Get holdings from BBAE."""
    await rate_limiter.wait_if_needed("BBAE")

    from session_manager import session_manager

    bbae = await session_manager.get_session("BBAE")
    if not bbae:
        print("No BBAE credentials supplied, skipping")
        return None

    return await _get_broker_holdings(bbae, "BBAE", ticker)


async def get_bbae_session(session_manager):
    """Get or create BBAE session."""
    if "bbae" not in session_manager._initialized:
        BBAE_USER = os.getenv("BBAE_USER")
        BBAE_PASS = os.getenv("BBAE_PASS")

        if not (BBAE_USER and BBAE_PASS):
            session_manager.sessions["bbae"] = None
            session_manager._initialized.add("bbae")
            return None

        try:
            bbae = await asyncio.to_thread(
                BBAEAPI, BBAE_USER, BBAE_PASS, creds_path="./tokens/"
            )
            if await _login_broker(bbae, "BBAE"):
                session_manager.sessions["bbae"] = bbae
                print("✓ BBAE session initialized")
            else:
                session_manager.sessions["bbae"] = None
        except Exception as e:
            print(f"✗ Failed to initialize BBAE session: {e}")
            session_manager.sessions["bbae"] = None

        session_manager._initialized.add("bbae")

    return session_manager.sessions.get("bbae")
