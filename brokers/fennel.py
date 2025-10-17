"""Fennel broker integration using official REST API."""

import os
import traceback
from .base import http_client


API_BASE = "https://api.fennel.com"


async def fennelTrade(side, qty, ticker, price):
    """Execute a trade on Fennel using official API."""
    from .session_manager import session_manager
    fennel_session = await session_manager.get_session("Fennel")
    if not fennel_session:
        print("No Fennel credentials supplied, skipping")
        return None

    access_token = fennel_session["access_token"]
    account_ids = fennel_session["account_ids"]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    # Map side and type to API enums
    # side: 1=BUY, 2=SELL
    # type: 1=MARKET, 2=LIMIT
    side_enum = 1 if side.lower() == "buy" else 2
    order_type = 1 if not price else 2  # Market if no price, Limit if price given

    for account_id in account_ids:
        order_data = {
            "account_id": account_id,
            "symbol": ticker.upper(),
            "shares": qty,
            "limit_price": float(price) if price else 0,
            "side": side_enum,
            "type": order_type,
            "time_in_force": 1,  # DAY
            "route": 0  # ROUTING_UNSPECIFIED (use default routing)
        }

        try:
            response = await http_client.post(
                f"{API_BASE}/order/create",
                headers=headers,
                json=order_data,
                timeout=30.0
            )

            if response.status_code == 200:
                action_str = "Bought" if side.lower() == "buy" else "Sold"
                order_type_str = "market" if not price else f"limit @ ${price}"
                print(f"{action_str} {qty} shares of {ticker} on Fennel account {account_id} ({order_type_str})")
            else:
                error_msg = response.text or "Unknown error"
                print(f"Failed to place order for {ticker} on Fennel account {account_id}: {error_msg}")

        except Exception as e:
            print(f"Error placing order for {ticker} on Fennel account {account_id}: {str(e)}")
            traceback.print_exc()


async def fennelGetHoldings(ticker=None):
    """Get holdings from Fennel using official API."""
    from .session_manager import session_manager
    fennel_session = await session_manager.get_session("Fennel")
    if not fennel_session:
        print("No Fennel credentials supplied, skipping")
        return None

    access_token = fennel_session["access_token"]
    account_ids = fennel_session["account_ids"]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json"
    }

    try:
        holdings_data = {}

        for account_id in account_ids:
            # Get positions for this account
            response = await http_client.post(
                f"{API_BASE}/portfolio/positions",
                headers=headers,
                json={"account_id": account_id},
                timeout=30.0
            )

            if response.status_code != 200:
                print(f"Failed to get holdings for Fennel account {account_id}: {response.text}")
                continue

            response_data = response.json()
            positions = response_data.get("positions", [])
            formatted_positions = []

            for position in positions:
                symbol = position.get("symbol", "")
                quantity = float(position.get("shares", 0))
                market_value = float(position.get("value", 0))

                # Cost basis is not directly provided in positions endpoint
                # Using market_value as an approximation
                cost_basis = market_value

                if ticker and symbol.upper() != ticker.upper():
                    continue

                formatted_positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "current_value": market_value
                })

            holdings_data[account_id] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Fennel holdings: {str(e)}")
        traceback.print_exc()
        return None


async def get_fennel_session(session_manager):
    """Get or create Fennel session using official API."""
    if "fennel" not in session_manager._initialized:
        access_token = os.getenv("FENNEL_ACCESS_TOKEN")

        if not access_token:
            session_manager.sessions["fennel"] = None
            session_manager._initialized.add("fennel")
            return None

        try:
            # Fetch account information to validate token and get account IDs
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json"
            }

            response = await http_client.get(
                f"{API_BASE}/accounts/info",
                headers=headers,
                timeout=30.0
            )

            if response.status_code == 200:
                accounts = response.json().get("accounts", [])
                account_ids = [account["id"] for account in accounts]

                session_manager.sessions["fennel"] = {
                    "access_token": access_token,
                    "account_ids": account_ids
                }
                print(f"✓ Fennel session initialized ({len(account_ids)} account(s))")
            else:
                error_msg = response.text or "Unknown error"
                print(f"✗ Failed to initialize Fennel session: {error_msg}")
                session_manager.sessions["fennel"] = None

        except Exception as e:
            print(f"✗ Failed to initialize Fennel session: {e}")
            traceback.print_exc()
            session_manager.sessions["fennel"] = None

        session_manager._initialized.add("fennel")

    return session_manager.sessions.get("fennel")
