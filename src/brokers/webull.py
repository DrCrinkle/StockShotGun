"""Webull broker integration using webull library.

IMPORTANT: Webull's API login is broken as of September 2025 (403 errors).

WORKAROUND: Use pre-obtained credentials from a browser session instead of logging in:
1. Install the Chrome extension: https://github.com/ImNotOssy/webull/releases/tag/1
2. Login to Webull in Chrome with the extension active
3. The extension will capture your credentials
4. Add these credentials to your .env file:
   - WEBULL_ACCESS_TOKEN
   - WEBULL_REFRESH_TOKEN
   - WEBULL_UUID
   - WEBULL_ACCOUNT_ID (supports comma-separated IDs for multiple accounts)
   - WEBULL_DID (optional, but recommended)

MULTIPLE ACCOUNTS:
To use multiple Webull accounts, provide comma-separated account IDs:
   WEBULL_ACCOUNT_ID=12345678,87654321,11111111

The integration will also attempt to discover additional accounts automatically.

If you have pre-obtained credentials, this integration will work. Otherwise, direct
login is not currently possible due to Webull API changes.

See: https://github.com/tedchou12/webull/issues/456
"""

import asyncio
import os
import traceback
from brokers.base import rate_limiter


async def _discover_accounts(
    wb, start_index=0, max_accounts=11, existing_account_ids=None
):
    """
    Discover accounts by probing indices. Returns list of account dicts.

    Args:
        wb: Webull client instance
        start_index: Starting index to probe (default 0)
        max_accounts: Maximum number of accounts to probe (default 11)
        existing_account_ids: Set of account IDs to skip (default None)

    Returns:
        List of dicts with keys: account_id, index
    """
    accounts = []
    existing_set = set(existing_account_ids) if existing_account_ids else set()

    for i in range(start_index, max_accounts):
        account_id = None
        try:
            account_id = await asyncio.to_thread(wb.get_account_id, i)
        except (IndexError, AttributeError, KeyError):
            # Expected: no more accounts at this index
            break
        except Exception as e:
            # Unexpected error - log but continue
            print(f"⚠ Unexpected error getting account at index {i}: {e}")
            break

        if account_id and account_id not in existing_set:
            accounts.append({"account_id": account_id, "index": i})
            existing_set.add(account_id)
        elif not account_id:
            # No account at this index - stop probing
            break

    return accounts


async def webullValidate(side, qty, ticker, price):
    """Validate order via Webull ticker lookup.

    Returns:
        (True, ""): Ticker is valid and tradeable
        (False, reason): Ticker not found
        (None, ""): No credentials
    """
    await rate_limiter.wait_if_needed("Webull")

    from brokers.session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        return (None, "")

    try:
        wb = webull_session["client"]
        ticker_id = await asyncio.to_thread(wb.get_ticker, ticker)
        if not ticker_id:
            return (False, "Ticker not found")
        return (True, "")
    except Exception as e:
        return (False, str(e).split("\n")[0][:100])


async def webullTrade(side, qty, ticker, price):
    """Execute a trade on Webull.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("Webull")

    from brokers.session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        print("No Webull credentials supplied, skipping")
        return None

    wb = webull_session["client"]
    accounts = webull_session["accounts"]

    success_count = 0
    failure_count = 0

    # Map side to action
    action = "BUY" if side.lower() == "buy" else "SELL"

    # Determine order type
    order_type = "MKT" if not price else "LMT"

    for account in accounts:
        account_id = account["account_id"]
        try:
            # Set the active account
            await asyncio.to_thread(wb.set_account_id, account_id)

            # Place order - wrap in try/except to catch library errors
            try:
                if order_type == "MKT":
                    response = await asyncio.to_thread(
                        wb.place_order,
                        stock=ticker.upper(),
                        action=action,
                        orderType=order_type,
                        quant=qty,
                    )
                else:
                    response = await asyncio.to_thread(
                        wb.place_order,
                        stock=ticker.upper(),
                        action=action,
                        orderType=order_type,
                        price=float(price),
                        quant=qty,
                    )
            except KeyError as ke:
                # The webull library is expecting a response key that doesn't exist
                # This usually means the API returned an error
                print(f"⚠ Webull API error for {ticker} on account {account_id}")
                print(
                    "  The stock might not be tradeable, or there's an issue with the order"
                )
                print(f"  Library error: Missing key '{ke}'")
                failure_count += 1
                continue
            except Exception as order_error:
                print(
                    f"Error placing order for {ticker} on Webull account {account_id}: {str(order_error)}"
                )
                traceback.print_exc()
                failure_count += 1
                continue

            # Check if order was successful
            if response and response.get("success"):
                action_str = "Bought" if side.lower() == "buy" else "Sold"
                order_type_str = "market" if not price else f"limit @ ${price}"
                print(
                    f"{action_str} {qty} shares of {ticker} on Webull account {account_id} ({order_type_str})"
                )
                success_count += 1
            else:
                error_msg = (
                    response.get("msg", "Unknown error") if response else "No response"
                )
                print(
                    f"Failed to place order for {ticker} on Webull account {account_id}: {error_msg}"
                )
                failure_count += 1

        except Exception as e:
            print(
                f"Error placing order for {ticker} on Webull account {account_id}: {str(e)}"
            )
            traceback.print_exc()
            failure_count += 1

    return success_count > 0


def _parse_webull_position(position, ticker=None):
    """
    Parse a Webull position from either v1 or v2 API format.

    Args:
        position: Position dict from Webull API (v1 or v2 format)
        ticker: Optional ticker symbol to filter by

    Returns:
        Dict with keys: symbol, quantity, cost_basis, current_value, or None if invalid/filtered
    """
    # Detect API version by checking for v1 format marker
    has_ticker = "ticker" in position and isinstance(position.get("ticker"), dict)
    has_items = "items" in position and isinstance(position.get("items"), list)

    if has_ticker:
        # v1 format: flat structure with ticker object
        symbol = position.get("ticker", {}).get("symbol", "")
        quantity = float(position.get("position", 0))
        market_value = float(position.get("marketValue", 0))
        cost_basis = float(position.get("costBasis", market_value))

    elif has_items:
        # v2 format: nested items array with lot-level detail
        items = position.get("items", [])
        if not items:
            return None

        # Extract symbol from first item (all items should have same symbol)
        symbol = items[0].get("symbol", "")
        if not symbol:
            return None

        # Aggregate quantities, cost basis, and unrealized P&L across all items (lots)
        total_quantity = 0.0
        total_cost = 0.0
        total_unrealized_pl = 0.0

        for item in items:
            # v2 uses strings for numeric values
            item_quantity = float(item.get("quantity", "0") or "0")
            item_cost_price = float(item.get("cost_price", "0") or "0")
            item_unrealized_pl = float(item.get("unrealized_profit_loss", "0") or "0")

            total_quantity += item_quantity
            total_cost += item_cost_price * item_quantity  # Weighted cost basis
            total_unrealized_pl += item_unrealized_pl

        quantity = total_quantity
        cost_basis = total_cost if total_quantity > 0 else 0.0
        # Calculate market value: cost_basis + unrealized_profit_loss
        market_value = cost_basis + total_unrealized_pl

    else:
        # Unknown format - skip this position
        return None

    # Skip zero quantity positions
    if quantity <= 0:
        return None

    # Filter by ticker if specified
    if ticker and symbol.upper() != ticker.upper():
        return None

    return {
        "symbol": symbol,
        "quantity": quantity,
        "cost_basis": cost_basis,
        "current_value": market_value,
    }


async def webullGetHoldings(ticker=None):
    """Get holdings from Webull."""
    await rate_limiter.wait_if_needed("Webull")

    from brokers.session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        print("No Webull credentials supplied, skipping")
        return None

    wb = webull_session["client"]
    accounts = webull_session["accounts"]

    try:
        holdings_data = {}

        for account in accounts:
            account_id = account["account_id"]

            try:
                # Set the active account
                await asyncio.to_thread(wb.set_account_id, account_id)

                # Get positions - try v1 first (default, stable response format), fall back to v2 if needed
                positions = None

                try:
                    positions = await asyncio.to_thread(wb.get_positions)
                except (KeyError, AttributeError, TypeError) as e:
                    # v1 failed - likely due to account type or API changes, try v2
                    try:
                        positions = await asyncio.to_thread(wb.get_positions, v2=True)
                    except Exception as v2_error:
                        print(
                            f"⚠ Both v1 and v2 get_positions failed for account {account_id}"
                        )
                        print(f"  v1 error: {type(e).__name__}: {e}")
                        print(f"  v2 error: {type(v2_error).__name__}: {v2_error}")
                        positions = None

                if not positions:
                    holdings_data[account_id] = []
                    continue

                formatted_positions = []

                for position in positions:
                    parsed = _parse_webull_position(position, ticker)
                    if parsed:
                        formatted_positions.append(parsed)

                holdings_data[account_id] = formatted_positions

            except Exception as e:
                print(
                    f"Error getting holdings for Webull account {account_id}: {str(e)}"
                )
                traceback.print_exc()
                holdings_data[account_id] = []

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Webull holdings: {str(e)}")
        traceback.print_exc()
        return None


async def get_webull_session(session_manager):
    """Get or create Webull session using pre-obtained credentials or traditional login."""
    if "webull" not in session_manager._initialized:
        # Check for pre-obtained credentials (recommended method)
        access_token = os.getenv("WEBULL_ACCESS_TOKEN")
        refresh_token = os.getenv("WEBULL_REFRESH_TOKEN")
        uuid = os.getenv("WEBULL_UUID")
        account_id = os.getenv("WEBULL_ACCOUNT_ID")
        device_id = os.getenv("WEBULL_DID")

        # Traditional credentials (for fallback, though login is currently broken)
        username = os.getenv("WEBULL_USER")
        password = os.getenv("WEBULL_PASS")
        trading_pin = os.getenv("WEBULL_TRADING_PIN")

        # Check if we have either type of credentials
        has_token_creds = all([access_token, refresh_token, uuid, account_id])
        has_login_creds = all([username, password])

        if not has_token_creds and not has_login_creds:
            session_manager.sessions["webull"] = None
            session_manager._initialized.add("webull")
            return None

        try:
            # Import webull library
            from webull import webull

            # Initialize Webull client
            wb = webull()

            # Set device ID if available
            if device_id:
                await asyncio.to_thread(wb.set_did, device_id)

            # Prefer pre-obtained credentials (api_login)
            if has_token_creds:
                print("Using pre-obtained Webull credentials (api_login)...")
                await asyncio.to_thread(
                    wb.api_login,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expire="2099-01-01T00:00:00.000+0000",  # Far future date
                    uuid=uuid,
                )
                # Manually set the account ID
                wb._account_id = account_id

                # Verify the session works
                test_account = None
                try:
                    test_account = await asyncio.to_thread(wb.get_account_id, 0)
                except (AttributeError, TypeError) as e:
                    print(f"⚠ Could not verify account ID (method error): {e}")
                except Exception:
                    # Other unexpected errors - continue with provided account_id
                    pass

                if test_account is None:
                    print(
                        "⚠ Warning: Could not verify account ID, using provided value"
                    )

            # Fallback to traditional login (will likely fail with 403)
            elif has_login_creds:
                print(
                    "⚠ Warning: Using traditional login (likely to fail due to Webull API changes)"
                )
                print("  Consider using pre-obtained credentials instead")
                print("  See: https://github.com/ImNotOssy/webull/releases/tag/1")

                await asyncio.to_thread(wb.login, username, password)

                if trading_pin:
                    await asyncio.to_thread(wb.get_trade_token, trading_pin)

                # Verify login
                test_account = await asyncio.to_thread(wb.get_account_id, 0)
                if not test_account:
                    raise Exception("Failed to get account ID after login")

            # Get all available accounts
            accounts = []

            # If using api_login with provided account IDs
            if has_token_creds and account_id:
                # Support comma-separated account IDs
                account_ids = [aid.strip() for aid in account_id.split(",")]
                print(f"Using {len(account_ids)} provided account ID(s)")

                for idx, aid in enumerate(account_ids):
                    accounts.append({"account_id": aid, "index": idx})

                # Try to discover additional accounts that weren't explicitly provided
                print("Attempting to discover additional accounts...")
                existing_ids = [acc["account_id"] for acc in accounts]
                discovered = await _discover_accounts(
                    wb, start_index=0, existing_account_ids=existing_ids
                )

                for account in discovered:
                    account["index"] = len(
                        accounts
                    )  # Update index based on final position
                    accounts.append(account)
                    print(f"  + Discovered additional account: {account['account_id']}")
            else:
                # Traditional login - discover all accounts
                accounts = await _discover_accounts(wb)

            if not accounts:
                raise Exception("No Webull accounts found")

            session_manager.sessions["webull"] = {"client": wb, "accounts": accounts}
            print(f"✓ Webull session initialized ({len(accounts)} account(s))")

        except Exception as e:
            print(f"✗ Failed to initialize Webull session: {e}")
            traceback.print_exc()
            session_manager.sessions["webull"] = None

        session_manager._initialized.add("webull")

    return session_manager.sessions.get("webull")
