"""Fennel broker integration."""

import os
import traceback
from fennel_invest_api import Fennel


async def fennelTrade(side, qty, ticker, price):
    """Execute a trade on Fennel."""
    from .session_manager import session_manager
    fennel = await session_manager.get_session("Fennel")
    if not fennel:
        print("No Fennel credentials supplied, skipping")
        return None

    account_ids = fennel.get_account_ids()
    for account_id in account_ids:
        order = fennel.place_order(
            account_id=account_id,
            ticker=ticker.upper(),
            quantity=qty,
            side=side,
            price="market",
        )

        if order.get('data', {}).get('createOrder') == 'pending':
            action_str = "Bought" if side == "buy" else "Sold"
            print(f"{action_str} {ticker} on Fennel account {account_id}")
        else:
            print(f"Failed to place order for {ticker} on Fennel account {account_id}")


async def fennelGetHoldings(ticker=None):
    """Get holdings from Fennel."""
    from .session_manager import session_manager
    fennel = await session_manager.get_session("Fennel")
    if not fennel:
        print("No Fennel credentials supplied, skipping")
        return None

    try:
        account_ids = fennel.get_account_ids()
        holdings_data = {}

        for account_id in account_ids:
            portfolio = fennel.get_stock_holdings(account_id)
            formatted_positions = []

            for position in portfolio:
                symbol = position['security']['ticker']
                quantity = float(position['investment']['ownedShares'])
                cost_basis = float(position['security']['currentStockPrice']) * quantity
                current_value = float(position['investment']['marketValue'])

                if ticker and symbol.upper() != ticker.upper():
                    continue

                formatted_positions.append({
                    "symbol": symbol,
                    "quantity": quantity,
                    "cost_basis": cost_basis,
                    "current_value": current_value
                })

            holdings_data[account_id] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Fennel holdings: {str(e)}")
        traceback.print_exc()
        return None


async def get_fennel_session(session_manager):
    """Get or create Fennel session."""
    if "fennel" not in session_manager._initialized:
        FENNEL_EMAIL = os.getenv("FENNEL_EMAIL")

        if not FENNEL_EMAIL:
            session_manager.sessions["fennel"] = None
            session_manager._initialized.add("fennel")
            return None

        try:
            fennel = Fennel(path="./tokens/")
            fennel.login(email=FENNEL_EMAIL, wait_for_code=True)
            session_manager.sessions["fennel"] = fennel
            print("✓ Fennel session initialized")
        except Exception as e:
            print(f"✗ Failed to initialize Fennel session: {e}")
            session_manager.sessions["fennel"] = None

        session_manager._initialized.add("fennel")

    return session_manager.sessions.get("fennel")
