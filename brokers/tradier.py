"""Tradier broker integration."""

import os
import httpx


async def tradierTrade(side, qty, ticker, price):
    """Execute a trade on Tradier."""
    from .session_manager import session_manager
    token = await session_manager.get_session("Tradier")
    if not token:
        print("Missing Tradier credentials, skipping")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    client = httpx.Client()

    response = client.get("https://api.tradier.com/v1/user/profile", headers=headers)

    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return False

    profile_data = response.json()
    accounts = profile_data.get("profile", {}).get("account", [])
    if not accounts:
        print("No accounts found.")
        return False

    TRADIER_ACCOUNT_ID = [account["account_number"] for account in accounts]

    # Order placement
    order_type = "limit" if price else "market"
    price_data = {"price": f"{price}"} if price else {}

    for account_id in TRADIER_ACCOUNT_ID:
        response = client.post(
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
            headers=headers
        )

        if response.status_code != 200:
            print(f"Error placing order on account {account_id}: {response.text}")
        else:
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on Tradier account {account_id}")

    client.close()


async def tradierGetHoldings(ticker=None):
    """Get holdings from Tradier."""
    from .session_manager import session_manager
    token = await session_manager.get_session("Tradier")
    if not token:
        print("Missing Tradier credentials, skipping")
        return None

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    client = httpx.Client()

    response = client.get("https://api.tradier.com/v1/user/profile", headers=headers)

    if response.status_code != 200:
        print(f"Error: {response.status_code} - {response.text}")
        return None

    profile_data = response.json()
    accounts = profile_data.get("profile", {}).get("account", [])
    if not accounts:
        print("No accounts found.")
        return None

    holdings_data = {}

    # Get holdings for each account
    for account in accounts:
        account_id = account["account_number"]
        response = client.get(
            f"https://api.tradier.com/v1/accounts/{account_id}/positions",
            headers=headers,
        )

        if response.status_code != 200:
            print(f"Error getting positions for account {account_id}: {response.text}")
            continue

        positions = response.json().get("positions", {}).get("position", [])

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
            quotes_response = client.get(
                "https://api.tradier.com/v1/markets/quotes",
                params={"symbols": ",".join(symbols)},
                headers=headers
            )
            quotes = quotes_response.json().get("quotes", {}).get("quote", [])
            if not isinstance(quotes, list):
                quotes = [quotes]
            quotes_dict = {quote.get("symbol"): quote.get("last") for quote in quotes}
        else:
            quotes_dict = {}

        holdings_data[account_id] = [{
            "symbol": pos.get("symbol"),
            "quantity": pos.get("quantity"),
            "cost_basis": pos.get("cost_basis"),
            "current_value": float(pos.get("quantity", 0)) * quotes_dict.get(pos.get("symbol"), 0)
        } for pos in positions]

    client.close()
    return holdings_data


async def get_tradier_session(session_manager):
    """Get or create Tradier session (token-based, no persistent session needed)."""
    if "tradier" not in session_manager._initialized:
        TRADIER_ACCESS_TOKEN = os.getenv("TRADIER_ACCESS_TOKEN")

        if not TRADIER_ACCESS_TOKEN:
            session_manager.sessions["tradier"] = None
            print("✗ No Tradier credentials supplied")
        else:
            session_manager.sessions["tradier"] = TRADIER_ACCESS_TOKEN
            print("✓ Tradier credentials available")

        session_manager._initialized.add("tradier")

    return session_manager.sessions.get("tradier")
