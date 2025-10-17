"""Public broker integration using official API."""

import os
import json
import traceback
from pathlib import Path
import httpx
import uuid


# Token cache file location
TOKEN_CACHE_FILE = Path("./tokens/public_token.json")


def _load_cached_token():
    """Load cached access token if available and valid."""
    if TOKEN_CACHE_FILE.exists():
        try:
            with open(TOKEN_CACHE_FILE, 'r') as f:
                data = json.load(f)
                return data.get('access_token')
        except Exception:
            return None
    return None


def _save_token(access_token):
    """Save access token to cache file."""
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(TOKEN_CACHE_FILE, 'w') as f:
            json.dump({'access_token': access_token}, f)
    except Exception as e:
        print(f"Warning: Failed to cache Public access token: {e}")


def _generate_access_token(api_secret):
    """Generate a new access token from API secret."""
    url = "https://api.public.com/userapiauthservice/personal/access-tokens"
    payload = {
        "validityInMinutes": 1440,  # 24 hours
        "secret": api_secret
    }

    try:
        response = httpx.post(url, json=payload, timeout=30.0)
        response.raise_for_status()
        data = response.json()
        access_token = data.get('accessToken')

        if access_token:
            _save_token(access_token)
            return access_token
        else:
            print("Error: No access token in response")
            return None
    except httpx.HTTPStatusError as e:
        print(f"Error generating Public access token: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        print(f"Error generating Public access token: {str(e)}")
        return None


def _get_accounts(access_token):
    """Fetch all account IDs for the authenticated user."""
    url = "https://api.public.com/userapigateway/trading/account"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        response = httpx.get(url, headers=headers, timeout=30.0)
        response.raise_for_status()
        data = response.json()

        # Extract account IDs from response
        # Response format: {"accounts": [{"accountId": "...", ...}, ...]}
        accounts = data.get('accounts', [])
        account_ids = [acc.get('accountId') for acc in accounts if acc.get('accountId')]

        return account_ids
    except httpx.HTTPStatusError as e:
        print(f"Error fetching Public accounts: {e.response.status_code} - {e.response.text}")
        return []
    except Exception as e:
        print(f"Error fetching Public accounts: {str(e)}")
        return []


async def publicTrade(side, qty, ticker, price):
    """Execute a trade on Public across all accounts."""
    from .session_manager import session_manager
    session = await session_manager.get_session("Public")
    if not session:
        print("No Public credentials supplied, skipping")
        return None

    access_token = session['access_token']
    account_ids = session['account_ids']

    if not account_ids:
        print("No Public accounts found")
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    # Prepare order payload according to Public.com API spec
    order_type = "MARKET" if price is None else "LIMIT"
    order_side = "BUY" if side.lower() == "buy" else "SELL"
    
    # Place order on each account with unique orderId per account
    for account_id in account_ids:
        # Generate unique orderId for each account to avoid deduplication conflicts
        order_id = str(uuid.uuid4())
        print(f"Generated orderId for account {account_id}: {order_id}")
        
        payload = {
            "orderId": order_id,
            "instrument": {
                "symbol": ticker,
                "type": "EQUITY"
            },
            "orderSide": order_side,
            "orderType": order_type,
            "expiration": {
                "timeInForce": "DAY"
            }
        }

        # Add quantity or amount based on order type
        if isinstance(qty, (int, float)) and qty > 0:
            payload["quantity"] = str(int(qty)) if qty == int(qty) else str(qty)
        else:
            # If qty is not a valid number, skip this account
            print(f"Invalid quantity for {ticker}: {qty}")
            continue

        # Add limit price for limit orders
        if price is not None:
            payload["limitPrice"] = str(price)
        try:
            url = f"https://api.public.com/userapigateway/trading/{account_id}/order"
            response = httpx.post(url, headers=headers, json=payload, timeout=30.0)
            response.raise_for_status()

            action_str = "Bought" if side.lower() == "buy" else "Sold"
            print(f"{action_str} {ticker} on Public account {account_id}")
        except httpx.HTTPStatusError as e:
            print(f"Failed to place order for {ticker} on Public account {account_id}: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            print(f"Failed to place order for {ticker} on Public account {account_id}: {str(e)}")


async def publicGetHoldings(ticker=None):
    """Get holdings from Public across all accounts."""
    from .session_manager import session_manager
    session = await session_manager.get_session("Public")
    if not session:
        print("No Public credentials supplied, skipping")
        return None

    access_token = session['access_token']
    account_ids = session['account_ids']

    if not account_ids:
        print("No Public accounts found")
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    holdings_data = {}

    try:
        for account_id in account_ids:
            url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
            response = httpx.get(url, headers=headers, timeout=30.0)
            response.raise_for_status()
            data = response.json()

            # Parse positions from response
            positions = data.get('positions', [])
            formatted_positions = []

            for position in positions:
                # Extract position details
                instrument = position.get('instrument', {})
                symbol = instrument.get('symbol', '')

                # Skip if filtering by ticker and doesn't match
                if ticker and symbol.upper() != ticker.upper():
                    continue

                # Extract values from API response structure
                quantity = float(position.get('quantity', 0) or 0)
                current_value = float(position.get('currentValue', 0) or 0)
                
                # Cost basis is a nested dict with totalCost field
                cost_basis_data = position.get('costBasis', {})
                cost_basis = float(cost_basis_data.get('totalCost', 0) or 0) if isinstance(cost_basis_data, dict) else 0

                formatted_positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "current_value": current_value
                })

            holdings_data[account_id] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Public holdings: {str(e)}")
        traceback.print_exc()
        return None


async def get_public_session(session_manager):
    """Get or create Public session with official API."""
    if "public" not in session_manager._initialized:
        PUBLIC_API_SECRET = os.getenv("PUBLIC_API_SECRET")

        if not PUBLIC_API_SECRET:
            session_manager.sessions["public"] = None
            session_manager._initialized.add("public")
            return None

        try:
            # Try to load cached token first
            access_token = _load_cached_token()

            # If no cached token, generate a new one
            if not access_token:
                access_token = _generate_access_token(PUBLIC_API_SECRET)

            if not access_token:
                raise Exception("Failed to obtain access token")

            # Fetch all account IDs
            account_ids = _get_accounts(access_token)

            if not account_ids:
                print("⚠️  Public authenticated but no accounts found")

            # Store session with token and account IDs
            session_manager.sessions["public"] = {
                'access_token': access_token,
                'account_ids': account_ids
            }

            print(f"✓ Public session initialized ({len(account_ids)} account(s) found)")
        except Exception as e:
            print(f"✗ Failed to initialize Public session: {e}")
            traceback.print_exc()
            session_manager.sessions["public"] = None

        session_manager._initialized.add("public")

    return session_manager.sessions.get("public")
