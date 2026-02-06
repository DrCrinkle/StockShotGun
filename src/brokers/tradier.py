"""Tradier broker integration."""

import os
from brokers.base import http_client, rate_limiter, api_cache, retry_operation


async def tradierTrade(side, qty, ticker, price):
    """Execute a trade on Tradier.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("Tradier")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Tradier")
    if not session:
        print("Missing Tradier credentials, skipping")
        return None

    # Extract token and cached account IDs
    token = session.get("token")
    account_ids = session.get("account_ids", [])

    if not account_ids:
        print("No Tradier accounts found.")
        return False

    success_count = 0
    failure_count = 0

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    # Order placement
    order_type = "limit" if price else "market"
    price_data = {"price": f"{price}"} if price else {}

    for account_id in account_ids:
        try:
            response = await http_client.post(
                f"https://api.tradier.com/v1/accounts/{account_id}/orders",
                data={
                    "class": "equity",
                    "symbol": ticker,
                    "side": side,
                    "quantity": qty,
                    "type": order_type,
                    "duration": "day",
                    **price_data,
                },
                headers=headers,
            )

            if response.status_code != 200:
                print(f"Error placing order on account {account_id}: {response.text}")
                failure_count += 1
            else:
                action_str = "Bought" if side == "buy" else "Sold"
                print(f"{action_str} {ticker} on Tradier account {account_id}")
                success_count += 1
        except Exception as e:
            print(
                f"Error placing order for {ticker} on Tradier account {account_id}: {str(e)}"
            )
            failure_count += 1

    return success_count > 0


async def tradierValidate(side, qty, ticker, price):
    """Validate order via Tradier quote check.

    Returns:
        (True, ""): Ticker is valid and tradeable
        (False, reason): Ticker not found
        (None, ""): No credentials
    """
    await rate_limiter.wait_if_needed("Tradier")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Tradier")
    if not session:
        return (None, "")

    try:
        token = session.get("token")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        response = await http_client.get(
            "https://api.tradier.com/v1/markets/quotes",
            params={"symbols": ticker},
            headers=headers,
        )
        if response.status_code != 200:
            return (False, f"Quote lookup failed (HTTP {response.status_code})")

        data = response.json()
        if not isinstance(data, dict):
            return (False, "Invalid quote response")

        # Tradier returns unmatched_symbols for unknown tickers
        unmatched = data.get("quotes", {}).get("unmatched_symbols")
        if unmatched:
            return (False, "Ticker not found")

        quote = data.get("quotes", {}).get("quote")
        if not quote:
            return (False, "Ticker not found")

        return (True, "")
    except Exception as e:
        return (False, str(e).split("\n")[0][:100])


async def tradierGetHoldings(ticker=None):
    """Get holdings from Tradier."""
    await rate_limiter.wait_if_needed("Tradier")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Tradier")
    if not session:
        print("Missing Tradier credentials, skipping")
        return None

    # Extract token and cached account IDs
    token = session.get("token")
    account_ids = session.get("account_ids", [])

    if not account_ids:
        print("No Tradier accounts found.")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    holdings_data = {}

    # Get holdings for each account
    for account_id in account_ids:
        response = await http_client.get(
            f"https://api.tradier.com/v1/accounts/{account_id}/positions",
            headers=headers,
        )

        if response.status_code != 200:
            print(f"Error getting positions for account {account_id}: {response.text}")
            continue

        # Parse JSON response - Tradier sometimes returns strings or non-dict responses
        try:
            data = response.json()
            if not isinstance(data, dict):
                print(
                    f"Unexpected response format from Tradier for account {account_id}: {data}"
                )
                continue
            positions = data.get("positions", {}).get("position", [])
        except (ValueError, AttributeError) as e:
            # Handle JSON decode errors or unexpected response types
            print(
                f"Error parsing Tradier response for account {account_id}: {e}. Response: {response.text[:200]}"
            )
            continue

        # Handle case where positions is None (no positions)
        if not positions:
            holdings_data[account_id] = []
            continue

        # Handle case where only one position is returned (comes as dict instead of list)
        if isinstance(positions, dict):
            positions = [positions]

        # If ticker is specified, filter for that ticker only
        if ticker:
            positions = [pos for pos in positions if pos.get("symbol") == ticker]

        # Get current quotes for all symbols
        symbols = [pos.get("symbol") for pos in positions]
        if symbols:
            quotes_response = await http_client.get(
                "https://api.tradier.com/v1/markets/quotes",
                params={"symbols": ",".join(symbols)},
                headers=headers,
            )
            # Parse JSON response - guard against non-dict responses
            try:
                quotes_data = quotes_response.json()
                if not isinstance(quotes_data, dict):
                    print(
                        f"Unexpected quotes response format from Tradier: {quotes_data}"
                    )
                    quotes = []
                else:
                    quotes = quotes_data.get("quotes", {}).get("quote", [])
                    if not isinstance(quotes, list):
                        quotes = [quotes] if quotes else []
            except (ValueError, AttributeError) as e:
                print(
                    f"Error parsing Tradier quotes response: {e}. Response: {quotes_response.text[:200]}"
                )
                quotes = []
            quotes_dict = {quote.get("symbol"): quote.get("last") for quote in quotes}
        else:
            quotes_dict = {}

        holdings_data[account_id] = [
            {
                "symbol": pos.get("symbol"),
                "quantity": pos.get("quantity"),
                "cost_basis": pos.get("cost_basis"),
                "current_value": float(pos.get("quantity", 0))
                * quotes_dict.get(pos.get("symbol"), 0),
            }
            for pos in positions
        ]

    return holdings_data


async def get_tradier_session(session_manager):
    """Get or create Tradier session and cache account IDs."""
    if "tradier" not in session_manager._initialized:
        TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

        if not TRADIER_ACCESS_TOKEN:
            session_manager.sessions["tradier"] = None
            print("✗ No Tradier credentials supplied")
        else:
            # Check if profile is cached
            cache_key = f"tradier_profile_{TRADIER_ACCESS_TOKEN[:10]}"
            cached_profile = api_cache.get(cache_key)

            if cached_profile:
                # Use cached profile data
                session_manager.sessions["tradier"] = cached_profile
                account_count = len(cached_profile.get("account_ids", []))
                print(
                    f"✓ Tradier credentials available ({account_count} account{'s' if account_count != 1 else ''}) [cached]"
                )
            else:
                # Fetch and cache account IDs during initialization
                async def _fetch_tradier_profile():
                    headers = {
                        "Authorization": f"Bearer {TRADIER_ACCESS_TOKEN}",
                        "Accept": "application/json",
                    }

                    response = await http_client.get(
                        "https://api.tradier.com/v1/user/profile", headers=headers
                    )

                    if response.status_code == 200:
                        profile_data = response.json()
                        if not isinstance(profile_data, dict):
                            raise Exception(
                                f"Unexpected profile response format: {profile_data}"
                            )

                        accounts = profile_data.get("profile", {}).get("account", [])
                        account_ids = (
                            [account["account_number"] for account in accounts]
                            if accounts
                            else []
                        )

                        return {
                            "token": TRADIER_ACCESS_TOKEN,
                            "account_ids": account_ids,
                        }
                    else:
                        raise Exception(
                            f"Failed to fetch profile: HTTP {response.status_code}"
                        )

                try:
                    session_data = await retry_operation(_fetch_tradier_profile)

                    # Cache the profile data
                    api_cache.set(cache_key, session_data)

                    session_manager.sessions["tradier"] = session_data
                    account_count = len(session_data.get("account_ids", []))
                    print(
                        f"✓ Tradier credentials available ({account_count} account{'s' if account_count != 1 else ''})"
                    )
                except (ValueError, AttributeError) as e:
                    print(f"Error parsing Tradier profile response: {e}")
                    session_manager.sessions["tradier"] = None
                except Exception as e:
                    print(f"✗ Error initializing Tradier session: {e}")
                    session_manager.sessions["tradier"] = None

        session_manager._initialized.add("tradier")

    return session_manager.sessions.get("tradier")
