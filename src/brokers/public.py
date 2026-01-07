"""Public broker integration using official API."""

import os
import json
import traceback
import time
from pathlib import Path
import uuid
from brokers.base import rate_limiter, http_client, retry_operation


# Token cache file location
TOKEN_CACHE_FILE = Path("./tokens/public_token.json")

# Token validity in minutes (24 hours)
TOKEN_VALIDITY_MINUTES = 1440
# Refresh token if it expires within this many minutes
TOKEN_REFRESH_BUFFER_MINUTES = 5


def _load_cached_token():
    """Load cached access token if available and valid (not expired)."""
    if TOKEN_CACHE_FILE.exists():
        try:
            with open(TOKEN_CACHE_FILE, "r") as f:
                data = json.load(f)
                access_token = data.get("access_token")
                expires_at = data.get("expires_at")

                # Check if token exists and is not expired (with buffer)
                if access_token and expires_at:
                    current_time = time.time()
                    # Refresh if expired or within buffer period
                    if current_time < (
                        expires_at - (TOKEN_REFRESH_BUFFER_MINUTES * 60)
                    ):
                        return access_token
                    # Token expired or about to expire
                    return None
                elif access_token:
                    # Legacy token without expiration - treat as invalid
                    return None
        except Exception:
            return None
    return None


def _save_token(access_token):
    """Save access token to cache file with expiration timestamp."""
    TOKEN_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Calculate expiration timestamp (current time + validity period)
        expires_at = time.time() + (TOKEN_VALIDITY_MINUTES * 60)

        with open(TOKEN_CACHE_FILE, "w") as f:
            json.dump({"access_token": access_token, "expires_at": expires_at}, f)
    except Exception as e:
        print(f"Warning: Failed to cache Public access token: {e}")


async def _generate_access_token(api_secret):
    """Generate a new access token from API secret."""
    url = "https://api.public.com/userapiauthservice/personal/access-tokens"
    payload = {"validityInMinutes": TOKEN_VALIDITY_MINUTES, "secret": api_secret}

    async def _fetch_token():
        """Fetch token with retry support."""
        response = await http_client.post(url, json=payload)
        response.raise_for_status()
        data = response.json()
        access_token = data.get("accessToken")

        if not access_token:
            raise Exception("No access token in response")

        return access_token

    try:
        access_token = await retry_operation(_fetch_token)
        _save_token(access_token)
        return access_token
    except Exception as e:
        print(f"Error generating Public access token: {str(e)}")
        return None


async def _refresh_token_if_needed(session_manager):
    """Refresh the Public access token if it's expired or about to expire."""
    PUBLIC_API_SECRET = os.getenv("PUBLIC_API_SECRET")
    if not PUBLIC_API_SECRET:
        return None

    # Check if token is expired
    cached_token = _load_cached_token()
    if not cached_token:
        # Generate new token
        new_token = await _generate_access_token(PUBLIC_API_SECRET)
        if new_token:
            # Update session with new token
            session = session_manager.sessions.get("public")
            if session:
                session["access_token"] = new_token
            return new_token
        return None

    return cached_token


async def _get_accounts(access_token):
    """Fetch all account IDs for the authenticated user."""
    url = "https://api.public.com/userapigateway/trading/account"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    try:
        response = await http_client.get(url, headers=headers)

        # Handle token expiration (401 Unauthorized)
        if response.status_code == 401:
            raise Exception("Token expired or invalid")

        response.raise_for_status()
        data = response.json()

        # Extract account IDs from response
        # Response format: {"accounts": [{"accountId": "...", ...}, ...]}
        accounts = data.get("accounts", [])
        account_ids = [acc.get("accountId") for acc in accounts if acc.get("accountId")]

        return account_ids
    except Exception as e:
        print(f"Error fetching Public accounts: {str(e)}")
        return []


async def publicTrade(side, qty, ticker, price):
    """Execute a trade on Public across all accounts.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials (broker skipped)
    """
    await rate_limiter.wait_if_needed("Public")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Public")
    if not session:
        print("No Public credentials supplied, skipping")
        return None

    # Refresh token if needed before trading
    access_token = await _refresh_token_if_needed(session_manager)
    if not access_token:
        print("Failed to refresh Public access token")
        return None

    # Update session with potentially refreshed token
    session["access_token"] = access_token
    account_ids = session["account_ids"]

    if not account_ids:
        print("No Public accounts found")
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Prepare order payload according to Public.com API spec
    order_type = "MARKET" if price is None else "LIMIT"
    order_side = "BUY" if side.lower() == "buy" else "SELL"

    success_count = 0
    failure_count = 0

    # Place order on each account with unique orderId per account
    for account_id in account_ids:
        # Generate unique orderId for each account to avoid deduplication conflicts
        order_id = str(uuid.uuid4())
        print(f"Generated orderId for account {account_id}: {order_id}")

        payload = {
            "orderId": order_id,
            "instrument": {"symbol": ticker, "type": "EQUITY"},
            "orderSide": order_side,
            "orderType": order_type,
            "expiration": {"timeInForce": "DAY"},
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
            response = await http_client.post(url, headers=headers, json=payload)

            # Handle token expiration - refresh and retry once
            if response.status_code == 401:
                print(f"Token expired, refreshing and retrying order for {ticker}...")
                new_token = await _refresh_token_if_needed(session_manager)
                if new_token:
                    session["access_token"] = new_token
                    headers["Authorization"] = f"Bearer {new_token}"
                    response = await http_client.post(
                        url, headers=headers, json=payload
                    )
                else:
                    print(f"Failed to refresh token for {ticker} order")
                    failure_count += 1
                    continue

            response.raise_for_status()

            action_str = "Bought" if side.lower() == "buy" else "Sold"
            print(f"{action_str} {ticker} on Public account {account_id}")
            success_count += 1
        except Exception as e:
            print(
                f"Failed to place order for {ticker} on Public account {account_id}: {str(e)}"
            )
            failure_count += 1

    # Return True if at least one account succeeded
    return success_count > 0


async def publicGetHoldings(ticker=None):
    """Get holdings from Public across all accounts."""
    await rate_limiter.wait_if_needed("Public")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("Public")
    if not session:
        print("No Public credentials supplied, skipping")
        return None

    # Refresh token if needed before fetching holdings
    access_token = await _refresh_token_if_needed(session_manager)
    if not access_token:
        print("Failed to refresh Public access token")
        return None

    # Update session with potentially refreshed token
    session["access_token"] = access_token
    account_ids = session["account_ids"]

    if not account_ids:
        print("No Public accounts found")
        return None

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    holdings_data = {}

    try:
        for account_id in account_ids:
            url = f"https://api.public.com/userapigateway/trading/{account_id}/portfolio/v2"
            response = await http_client.get(url, headers=headers)

            # Handle token expiration - refresh and retry once
            if response.status_code == 401:
                print("Token expired, refreshing and retrying holdings fetch...")
                new_token = await _refresh_token_if_needed(session_manager)
                if new_token:
                    session["access_token"] = new_token
                    headers["Authorization"] = f"Bearer {new_token}"
                    response = await http_client.get(url, headers=headers)
                else:
                    print("Failed to refresh token for holdings fetch")
                    continue

            response.raise_for_status()
            data = response.json()

            # Parse positions from response
            positions = data.get("positions", [])
            formatted_positions = []

            for position in positions:
                # Extract position details
                instrument = position.get("instrument", {})
                symbol = instrument.get("symbol", "")

                # Skip if filtering by ticker and doesn't match
                if ticker and symbol.upper() != ticker.upper():
                    continue

                # Extract values from API response structure
                quantity = float(position.get("quantity", 0) or 0)
                current_value = float(position.get("currentValue", 0) or 0)

                # Cost basis is a nested dict with totalCost field
                cost_basis_data = position.get("costBasis", {})
                cost_basis = (
                    float(cost_basis_data.get("totalCost", 0) or 0)
                    if isinstance(cost_basis_data, dict)
                    else 0
                )

                formatted_positions.append(
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "current_value": current_value,
                    }
                )

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
            # Try to load cached token first (checks expiration)
            access_token = _load_cached_token()

            # If no cached token or expired, generate a new one
            if not access_token:
                access_token = await _generate_access_token(PUBLIC_API_SECRET)

            if not access_token:
                raise Exception("Failed to obtain access token")

            # Fetch all account IDs (with retry on token expiration)
            account_ids = await _get_accounts(access_token)

            # If accounts fetch failed due to expired token, refresh and retry
            if not account_ids:
                # Token might have expired between check and use
                access_token = await _refresh_token_if_needed(session_manager)
                if access_token:
                    account_ids = await _get_accounts(access_token)

            if not account_ids:
                print("⚠️  Public authenticated but no accounts found")

            # Store session with token and account IDs
            session_manager.sessions["public"] = {
                "access_token": access_token,
                "account_ids": account_ids,
            }

            print(f"✓ Public session initialized ({len(account_ids)} account(s) found)")
        except Exception as e:
            print(f"✗ Failed to initialize Public session: {e}")
            traceback.print_exc()
            session_manager.sessions["public"] = None

        session_manager._initialized.add("public")

    return session_manager.sessions.get("public")
