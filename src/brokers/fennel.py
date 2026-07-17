"""Fennel broker integration using official REST API."""

import os
import traceback
from brokers.base import http_client, rate_limiter, retry_operation


API_BASE = "https://api.fennel.com"


async def _fennel_submit_order(access_token, account_id, side, qty, ticker, price):
    """POST one order to `/order/create` for exactly ONE Fennel account_id.

    The single SDK call shared by both the account-scoped path
    (`fennelTrade(..., account_id=...)`) and the legacy blind loop below —
    same headers, same enum mapping, same endpoint. Never raises; returns
    True/False so callers can tally success without duplicating
    error-handling.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Map side and type to API enums
    # side: 1=BUY, 2=SELL
    # order_type: 1=MARKET, 2=LIMIT
    side_enum = 1 if side.lower() == "buy" else 2
    order_type = 1 if not price else 2  # Market if no price, Limit if price given

    order_data = {
        "account_id": account_id,
        "symbol": ticker.upper(),
        "shares": qty,
        "limit_price": float(price) if price else 0,
        "side": side_enum,
        "type": order_type,
        "time_in_force": 1,  # DAY
        "route": "EXCHANGE_ATS_SDP",
    }

    try:
        response = await http_client.post(
            f"{API_BASE}/order/create",
            headers=headers,
            json=order_data,
            timeout=30.0,
        )

        if response.status_code == 200:
            action_str = "Bought" if side.lower() == "buy" else "Sold"
            order_type_str = "market" if not price else f"limit @ ${price}"
            print(
                f"{action_str} {qty} shares of {ticker} on Fennel account {account_id} ({order_type_str})"
            )
            return True
        else:
            error_msg = response.text or "Unknown error"
            print(
                f"Failed to place order for {ticker} on Fennel account {account_id}: {error_msg}"
            )
            return False

    except Exception as e:
        print(
            f"Error placing order for {ticker} on Fennel account {account_id}: {str(e)}"
        )
        traceback.print_exc()
        return False


async def fennelTrade(side, qty, ticker, price, account_id=None):
    """Execute a trade on Fennel using official API.

    Args:
        account_id: when given (the engine's account-scoped dispatch path —
            see `brokers/registry.py`'s Fennel `BrokerSpec.account_scoped_trade`
            and `execution/in_process.py:place_at_broker`), places exactly
            ONE order for that account and returns a bool for that single
            call. No internal loop runs in this branch.

            When omitted (`None`), falls back to the legacy blind behavior:
            fans out over every account_id cached in the session. Nothing in
            this codebase calls `fennelTrade` this way anymore — the
            registry now marks Fennel `account_scoped_trade=True`, so
            `place_at_broker` always supplies `account_id` — but the branch
            is kept for any direct/legacy caller so it still behaves as
            documented rather than raising.

    Returns:
        True: Trade executed successfully (on the given account, or on at
            least one account in the legacy fan-out)
        False: Trade failed (on the given account, or on every account in
            the legacy fan-out)
        None: No credentials (broker skipped)
    """
    await rate_limiter.wait_if_needed("Fennel")

    from brokers.session_manager import session_manager

    fennel_session = await session_manager.get_session("Fennel")
    if not fennel_session:
        print("No Fennel credentials supplied, skipping")
        return None

    access_token = fennel_session["access_token"]

    if account_id is not None:
        # Account-scoped dispatch: one call, one account, one live order.
        # The old internal loop below never runs on this path.
        return await _fennel_submit_order(
            access_token, account_id, side, qty, ticker, price
        )

    # Legacy blind path — no account_id supplied. Fans out internally over
    # every session account, same as before ADR 0006 completion.
    account_ids = fennel_session["account_ids"]

    success_count = 0
    failure_count = 0

    for aid in account_ids:
        ok = await _fennel_submit_order(access_token, aid, side, qty, ticker, price)
        if ok:
            success_count += 1
        else:
            failure_count += 1

    # Return True if at least one account succeeded
    return success_count > 0


async def fennelGetHoldings(ticker=None):
    """Get holdings from Fennel using official API."""
    await rate_limiter.wait_if_needed("Fennel")

    from brokers.session_manager import session_manager

    fennel_session = await session_manager.get_session("Fennel")
    if not fennel_session:
        print("No Fennel credentials supplied, skipping")
        return None

    access_token = fennel_session["access_token"]
    account_ids = fennel_session["account_ids"]

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    try:
        holdings_data = {}

        for account_id in account_ids:
            # Get positions for this account
            response = await http_client.post(
                f"{API_BASE}/portfolio/positions",
                headers=headers,
                json={"account_id": account_id},
                timeout=30.0,
            )

            if response.status_code != 200:
                print(
                    f"Failed to get holdings for Fennel account {account_id}: {response.text}"
                )
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

                formatted_positions.append(
                    {
                        "symbol": symbol,
                        "quantity": quantity,
                        "cost_basis": cost_basis,
                        "current_value": market_value,
                    }
                )

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

        async def _fetch_fennel_accounts():
            headers = {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }

            response = await http_client.get(
                f"{API_BASE}/accounts/info", headers=headers, timeout=30.0
            )

            if response.status_code == 200:
                accounts = response.json().get("accounts", [])
                account_ids = [account["id"] for account in accounts]
                return account_ids
            else:
                error_msg = response.text or "Unknown error"
                raise Exception(f"Failed to fetch accounts: {error_msg}")

        try:
            account_ids = await retry_operation(_fetch_fennel_accounts)
            session_manager.sessions["fennel"] = {
                "access_token": access_token,
                "account_ids": account_ids,
            }
            print(f"✓ Fennel session initialized ({len(account_ids)} account(s))")
        except Exception as e:
            print(f"✗ Failed to initialize Fennel session: {e}")
            traceback.print_exc()
            session_manager.sessions["fennel"] = None

        session_manager._initialized.add("fennel")

    return session_manager.sessions.get("fennel")
