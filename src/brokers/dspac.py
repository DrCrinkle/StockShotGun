"""DSPAC broker integration."""

import os
import asyncio
import traceback
from dspac_invest_api import DSPACAPI
from brokers.base import _login_broker, _get_broker_holdings, rate_limiter


async def dspacTrade(side, qty, ticker, price):
    """Execute a trade on DSPAC.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("DSPAC")

    from brokers.session_manager import session_manager

    dspac = await session_manager.get_session("DSPAC")
    if not dspac:
        print("No DSPAC credentials supplied, skipping")
        return None

    success_count = 0
    failure_count = 0

    try:
        account_info = await asyncio.to_thread(dspac.get_account_info)
        account_number = account_info.get("Data").get("accountNumber")

        if not account_number:
            print("Failed to retrieve account number from DSPAC.")
            return False

        if side == "buy":
            response = await asyncio.to_thread(
                dspac.execute_buy,
                ticker,
                qty,
                account_number,
                dry_run=False,
            )
        elif side == "sell":
            holdings_response = await asyncio.to_thread(
                dspac.check_stock_holdings,
                ticker,
                account_number,
            )
            available_qty = holdings_response.get("Data").get("enableAmount", 0)

            if int(available_qty) < qty:
                print(
                    f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}"
                )
                return False

            response = await asyncio.to_thread(
                dspac.execute_sell,
                ticker,
                qty,
                account_number,
                price,
                dry_run=False,
            )
        else:
            print(f"Invalid trade side: {side}")
            return False

        if response.get("Outcome") == "Success":
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {qty} shares of {ticker} on DSPAC.")
            success_count += 1
        else:
            print(f"Failed to {side} {ticker}: {response.get('Message')}")
            failure_count += 1
    except Exception as e:
        print(f"Error trading {ticker} on DSPAC: {str(e)}")
        traceback.print_exc()
        failure_count += 1

    return success_count > 0


async def dspacGetHoldings(ticker=None):
    """Get holdings from DSPAC."""
    await rate_limiter.wait_if_needed("DSPAC")

    from brokers.session_manager import session_manager

    dspac = await session_manager.get_session("DSPAC")
    if not dspac:
        print("No DSPAC credentials supplied, skipping")
        return None

    return await _get_broker_holdings(dspac, "DSPAC", ticker)


async def get_dspac_session(session_manager):
    """Get or create DSPAC session."""
    if "dspac" not in session_manager._initialized:
        DSPAC_USER = os.getenv("DSPAC_USER")
        DSPAC_PASS = os.getenv("DSPAC_PASS")

        if not (DSPAC_USER and DSPAC_PASS):
            session_manager.sessions["dspac"] = None
            session_manager._initialized.add("dspac")
            return None

        try:
            dspac = await asyncio.to_thread(
                DSPACAPI, DSPAC_USER, DSPAC_PASS, creds_path="./tokens/"
            )
            if await _login_broker(dspac, "DSPAC"):
                session_manager.sessions["dspac"] = dspac
                print("✓ DSPAC session initialized")
            else:
                session_manager.sessions["dspac"] = None
        except Exception as e:
            print(f"✗ Failed to initialize DSPAC session: {e}")
            session_manager.sessions["dspac"] = None

        session_manager._initialized.add("dspac")

    return session_manager.sessions.get("dspac")
