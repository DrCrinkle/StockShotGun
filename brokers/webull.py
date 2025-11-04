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

import os
import traceback


async def webullTrade(side, qty, ticker, price):
    """Execute a trade on Webull."""
    from .session_manager import session_manager
    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        print("No Webull credentials supplied, skipping")
        return None

    wb = webull_session["client"]
    accounts = webull_session["accounts"]

    # Map side to action
    action = "BUY" if side.lower() == "buy" else "SELL"

    # Determine order type
    order_type = "MKT" if not price else "LMT"

    for account in accounts:
        account_id = account["account_id"]
        try:
            # Set the active account
            wb.set_account_id(account_id)

            # Place order - wrap in try/except to catch library errors
            try:
                if order_type == "MKT":
                    response = wb.place_order(
                        stock=ticker.upper(),
                        action=action,
                        orderType=order_type,
                        quant=qty
                    )
                else:
                    response = wb.place_order(
                        stock=ticker.upper(),
                        action=action,
                        orderType=order_type,
                        price=float(price),
                        quant=qty
                    )
            except KeyError as ke:
                # The webull library is expecting a response key that doesn't exist
                # This usually means the API returned an error
                print(f"⚠ Webull API error for {ticker} on account {account_id}")
                print("  The stock might not be tradeable, or there's an issue with the order")
                print(f"  Library error: Missing key '{ke}'")
                continue
            except Exception as order_error:
                print(f"Error placing order for {ticker} on Webull account {account_id}: {str(order_error)}")
                traceback.print_exc()
                continue

            # Check if order was successful
            if response and response.get("success"):
                action_str = "Bought" if side.lower() == "buy" else "Sold"
                order_type_str = "market" if not price else f"limit @ ${price}"
                print(f"{action_str} {qty} shares of {ticker} on Webull account {account_id} ({order_type_str})")
            else:
                error_msg = response.get("msg", "Unknown error") if response else "No response"
                print(f"Failed to place order for {ticker} on Webull account {account_id}: {error_msg}")

        except Exception as e:
            print(f"Error placing order for {ticker} on Webull account {account_id}: {str(e)}")
            traceback.print_exc()


async def webullGetHoldings(ticker=None):
    """Get holdings from Webull."""
    from .session_manager import session_manager
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
                wb.set_account_id(account_id)

                # Get positions - try v1 first, fall back to v2 if needed
                try:
                    positions = wb.get_positions()
                except:
                    positions = wb.get_positions(v2=True)

                if not positions:
                    holdings_data[account_id] = []
                    continue

                formatted_positions = []

                for position in positions:
                    symbol = position.get("ticker", {}).get("symbol", "")
                    quantity = float(position.get("position", 0))

                    # Skip zero quantity positions
                    if quantity <= 0:
                        continue

                    # Filter by ticker if specified
                    if ticker and symbol.upper() != ticker.upper():
                        continue

                    market_value = float(position.get("marketValue", 0))
                    cost_basis = float(position.get("costBasis", market_value))

                    formatted_positions.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "current_value": market_value
                    })

                holdings_data[account_id] = formatted_positions

            except Exception as e:
                print(f"Error getting holdings for Webull account {account_id}: {str(e)}")
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
                wb.set_did(device_id)

            # Prefer pre-obtained credentials (api_login)
            if has_token_creds:
                print("Using pre-obtained Webull credentials (api_login)...")
                wb.api_login(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expire='2099-01-01T00:00:00.000+0000',  # Far future date
                    uuid=uuid
                )
                # Manually set the account ID
                wb._account_id = account_id

                # Verify the session works
                try:
                    test_account = wb.get_account_id(0)
                    if not test_account:
                        print("⚠ Warning: Could not verify account ID, using provided value")
                except:
                    # If verification fails, still use the provided account_id
                    pass

            # Fallback to traditional login (will likely fail with 403)
            elif has_login_creds:
                print("⚠ Warning: Using traditional login (likely to fail due to Webull API changes)")
                print("  Consider using pre-obtained credentials instead")
                print("  See: https://github.com/ImNotOssy/webull/releases/tag/1")

                wb.login(username, password)

                if trading_pin:
                    wb.get_trade_token(trading_pin)

                # Verify login
                test_account = wb.get_account_id(0)
                if not test_account:
                    raise Exception("Failed to get account ID after login")

            # Get all available accounts
            accounts = []

            # If using api_login with provided account IDs
            if has_token_creds and account_id:
                # Support comma-separated account IDs
                account_ids = [aid.strip() for aid in account_id.split(',')]
                print(f"Using {len(account_ids)} provided account ID(s)")

                for idx, aid in enumerate(account_ids):
                    accounts.append({
                        "account_id": aid,
                        "index": idx
                    })

                # Try to discover additional accounts that weren't explicitly provided
                print("Attempting to discover additional accounts...")
                MAX_ACCOUNTS = 11  # Webull supports up to 11 accounts
                for i in range(MAX_ACCOUNTS):
                    try:
                        discovered_account_id = wb.get_account_id(i)
                        if discovered_account_id:
                            # Only add if not already in our list
                            if not any(acc["account_id"] == discovered_account_id for acc in accounts):
                                accounts.append({
                                    "account_id": discovered_account_id,
                                    "index": len(accounts)
                                })
                                print(f"  + Discovered additional account: {discovered_account_id}")
                    except:
                        break  # No more accounts
            else:
                # Traditional login - discover all accounts
                MAX_ACCOUNTS = 11  # Webull supports up to 11 accounts
                for i in range(MAX_ACCOUNTS):
                    try:
                        discovered_account_id = wb.get_account_id(i)
                        if discovered_account_id:
                            accounts.append({
                                "account_id": discovered_account_id,
                                "index": i
                            })
                    except:
                        break  # No more accounts

            if not accounts:
                raise Exception("No Webull accounts found")

            session_manager.sessions["webull"] = {
                "client": wb,
                "accounts": accounts
            }
            print(f"✓ Webull session initialized ({len(accounts)} account(s))")

        except Exception as e:
            print(f"✗ Failed to initialize Webull session: {e}")
            traceback.print_exc()
            session_manager.sessions["webull"] = None

        session_manager._initialized.add("webull")

    return session_manager.sessions.get("webull")
