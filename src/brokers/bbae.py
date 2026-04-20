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

    from brokers.session_manager import session_manager

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
            def _validate_and_buy():
                v = bbae.validate_buy(ticker, qty, 1, account_number)
                return bbae.execute_buy(ticker, qty, account_number,
                    dry_run=False, validation_response=v)
            response = await asyncio.to_thread(_validate_and_buy)
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

            def _validate_and_sell():
                bbae.validate_sell(ticker, qty, account_number)
                return bbae.execute_sell(ticker, qty, account_number, price, dry_run=False)
            response = await asyncio.to_thread(_validate_and_sell)
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


async def bbaeValidate(side, qty, ticker, price):
    """Validate order via BBAE dry-run.

    Returns:
        (True, ""): Order is valid
        (False, reason): Order would fail
        (None, ""): No credentials
    """
    await rate_limiter.wait_if_needed("BBAE")

    from brokers.session_manager import session_manager

    bbae = await session_manager.get_session("BBAE")
    if not bbae:
        return (None, "")

    try:
        account_info = await asyncio.to_thread(bbae.get_account_info)
        account_number = account_info.get("Data", {}).get("accountNumber")
        if not account_number:
            return (False, "No account found")

        if side == "buy":
            def _validate_and_dry_run():
                v = bbae.validate_buy(ticker, qty, 1, account_number)
                return bbae.execute_buy(ticker, qty, account_number,
                    dry_run=True, validation_response=v)
            response = await asyncio.to_thread(_validate_and_dry_run)
        else:
            holdings_response = await asyncio.to_thread(
                bbae.check_stock_holdings, ticker, account_number
            )
            available_qty = holdings_response.get("Data", {}).get("enableAmount", 0)
            if int(available_qty) < qty:
                return (False, f"Insufficient shares ({available_qty} available)")
            validation = await asyncio.to_thread(
                bbae.validate_sell, ticker, qty, account_number
            )
            if validation.get("Outcome") != "Success":
                return (False, validation.get("Message", "Sell validation failed")[:100])
            return (True, "")

        if response.get("Outcome") == "Success":
            return (True, "")
        return (False, response.get("Message", "Validation failed")[:100])
    except Exception as e:
        return (False, str(e).split("\n")[0][:100])


async def bbaeGetHoldings(ticker=None):
    """Get holdings from BBAE."""
    await rate_limiter.wait_if_needed("BBAE")

    from brokers.session_manager import session_manager

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
