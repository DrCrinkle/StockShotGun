"""Robinhood broker integration."""

import asyncio
from typing import Any, Dict, List, Optional, Tuple

import pyotp
import robin_stocks.robinhood as rh
from brokers.base import retry_operation, rate_limiter, api_cache


async def _get_robinhood_accounts() -> List[dict]:
    """Fetch and cache the user's Robinhood accounts."""
    cache_key = "robinhood_accounts"
    cached_accounts = api_cache.get(cache_key)
    if cached_accounts is not None:
        return cached_accounts

    accounts = await asyncio.to_thread(
        rh.account.load_account_profile,
        dataType="results",
    )
    api_cache.set(cache_key, accounts)
    return accounts


async def _get_symbol_for_instrument(instrument_url: str) -> str:
    """Convert an instrument URL to a symbol using a simple cache."""
    cache_key = f"robinhood_instrument_{instrument_url}"
    cached_symbol = api_cache.get(cache_key)
    if cached_symbol is not None:
        return cached_symbol

    symbol = await asyncio.to_thread(rh.get_symbol_by_url, instrument_url)
    api_cache.set(cache_key, symbol)
    return symbol


async def _get_latest_prices(symbols: List[str]) -> Dict[str, float]:
    """Fetch latest prices for a list of symbols in a single API call."""
    if not symbols:
        return {}

    prices = await asyncio.to_thread(rh.get_latest_price, symbols)
    price_map: Dict[str, float] = {}
    for symbol, price in zip(symbols, prices):
        try:
            price_map[symbol] = float(price)
        except (TypeError, ValueError):
            price_map[symbol] = 0.0
    return price_map


async def _submit_order_for_account(
    account: dict,
    order_function,
    base_order_args: Dict[str, Any],
    side: str,
    ticker: str,
) -> bool:
    """Submit a single order for an account."""
    account_number = account["account_number"]
    brokerage_account_type = account["brokerage_account_type"]
    order_args = dict(base_order_args)
    order_args["account_number"] = account_number

    try:
        await asyncio.to_thread(order_function, **order_args)
        action_str = "Bought" if side == "buy" else "Sold"
        print(
            f"{action_str} {ticker} on Robinhood "
            f"{brokerage_account_type} account {account_number}"
        )
        return True
    except Exception as exc:
        print(f"Failed to {side} {ticker} on Robinhood account {account_number}: {exc}")
        return False


async def _collect_account_holdings(
    account: dict,
    ticker_filter: Optional[str],
) -> Tuple[str, Optional[List[dict]]]:
    """Retrieve and format holdings for a single account."""
    account_number = account["account_number"]
    positions = await asyncio.to_thread(
        rh.get_open_stock_positions,
        account_number=account_number,
    )

    if not positions:
        return account_number, None

    symbol_tasks = [
        asyncio.create_task(_get_symbol_for_instrument(position["instrument"]))
        for position in positions
    ]
    symbols = await asyncio.gather(*symbol_tasks)

    formatted_positions: List[dict] = []
    unique_symbols = set()

    for position, symbol in zip(positions, symbols):
        if not symbol:
            continue

        quantity = float(position["quantity"])
        if ticker_filter and symbol.upper() != ticker_filter.upper():
            continue

        cost_basis = float(position["average_buy_price"]) * quantity
        formatted_positions.append(
            {
                "symbol": symbol,
                "quantity": quantity,
                "cost_basis": cost_basis,
            }
        )
        unique_symbols.add(symbol)

    price_map = await _get_latest_prices(list(unique_symbols))
    for entry in formatted_positions:
        current_price = price_map.get(entry["symbol"], 0.0)
        entry["current_value"] = current_price * entry["quantity"]

    return account_number, formatted_positions


async def robinTrade(side, qty, ticker, price):
    """Execute a trade on Robinhood.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials (broker skipped)
    """
    await rate_limiter.wait_if_needed("Robinhood")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Robinhood")
    if not session:
        print("No Robinhood credentials supplied, skipping")
        return None

    all_accounts = await _get_robinhood_accounts()

    if side not in ["buy", "sell"]:
        print(f"Invalid side: {side}")
        return False

    if side == "buy":
        order_function = rh.order_buy_limit if price else rh.order_buy_market
    else:
        order_function = rh.order_sell_limit if price else rh.order_sell_market

    base_order_args = {
        "symbol": ticker,
        "quantity": qty,
        "timeInForce": "gfd",
    }
    if price:
        base_order_args["limitPrice"] = price

    order_tasks = [
        asyncio.create_task(
            _submit_order_for_account(
                account,
                order_function,
                base_order_args,
                side,
                ticker,
            )
        )
        for account in all_accounts
    ]

    if not order_tasks:
        print("No Robinhood accounts available for trading")
        return False

    task_results = await asyncio.gather(*order_tasks, return_exceptions=True)
    success_count = sum(result is True for result in task_results)

    # Return True if at least one account succeeded
    return success_count > 0


async def robinGetHoldings(ticker=None):
    """Get holdings from Robinhood."""
    await rate_limiter.wait_if_needed("Robinhood")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Robinhood")
    if not session:
        print("No Robinhood credentials supplied, skipping")
        return None

    holdings_data = {}
    all_accounts = await _get_robinhood_accounts()

    account_tasks = [
        asyncio.create_task(_collect_account_holdings(account, ticker))
        for account in all_accounts
    ]

    if not account_tasks:
        return holdings_data

    account_results = await asyncio.gather(*account_tasks, return_exceptions=True)
    for result in account_results:
        if isinstance(result, BaseException):
            print(f"Failed to load holdings for a Robinhood account: {result}")
            continue

        account_number, formatted_positions = result
        if formatted_positions is not None:
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
                    rh.login,
                    ROBINHOOD_USER,
                    ROBINHOOD_PASS,
                    mfa_code=mfa,
                    pickle_path="./tokens/",
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
