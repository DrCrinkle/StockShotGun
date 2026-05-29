"""Shared factory for Redbridge Securities brokers (BBAE, DSPAC).

BBAE and DSPAC are both Redbridge Securities brokers and expose identical
APIs through the ``bbae_invest_api`` / ``dspac_invest_api`` SDKs. Rather than
maintain two byte-identical modules, this factory produces the
trade / validate / holdings / session functions for either broker from a
single implementation, so a fix to one can never silently skip the other.
"""

import asyncio
import os
import traceback

from brokers.base import _login_broker, _get_broker_holdings, rate_limiter


def make_redbridge_broker(name, api_class, *, session_key=None, env_user=None, env_pass=None):
    """Build the four public broker functions for a Redbridge broker.

    Args:
        name: Display name (e.g. "BBAE", "DSPAC").
        api_class: SDK client class (e.g. BBAEAPI, DSPACAPI).
        session_key: Session-dict key; defaults to ``name.lower()``.
        env_user / env_pass: Credential env var names; default ``{NAME}_USER`` / ``{NAME}_PASS``.

    Returns:
        (trade, validate, get_holdings, get_session) coroutine functions.
    """
    session_key = session_key or name.lower()
    env_user = env_user or f"{name.upper()}_USER"
    env_pass = env_pass or f"{name.upper()}_PASS"

    async def trade(side, qty, ticker, price):
        """Execute a trade on the broker.

        Returns:
            True: Trade executed successfully on at least one account
            False: Trade failed on all accounts
            None: No credentials supplied
        """
        await rate_limiter.wait_if_needed(name)

        from brokers.session_manager import session_manager

        broker = await session_manager.get_session(name)
        if not broker:
            print(f"No {name} credentials supplied, skipping")
            return None

        success_count = 0
        failure_count = 0

        try:
            account_info = await asyncio.to_thread(broker.get_account_info)
            account_number = account_info.get("Data").get("accountNumber")

            if not account_number:
                print(f"Failed to retrieve account number from {name}.")
                return False

            if side == "buy":
                def _validate_and_buy():
                    v = broker.validate_buy(ticker, qty, 1, account_number)
                    return broker.execute_buy(ticker, qty, account_number,
                        dry_run=False, validation_response=v)
                response = await asyncio.to_thread(_validate_and_buy)
            elif side == "sell":
                holdings_response = await asyncio.to_thread(
                    broker.check_stock_holdings, ticker, account_number
                )
                available_qty = holdings_response.get("Data").get("enableAmount", 0)

                if int(available_qty) < qty:
                    print(
                        f"Not enough shares to sell. Available: {available_qty}, Requested: {qty}"
                    )
                    return False

                def _validate_and_sell():
                    broker.validate_sell(ticker, qty, account_number)
                    return broker.execute_sell(ticker, qty, account_number, price, dry_run=False)
                response = await asyncio.to_thread(_validate_and_sell)
            else:
                print(f"Invalid trade side: {side}")
                return False

            if response.get("Outcome") == "Success":
                action_str = "Bought" if side == "buy" else "Sold"
                print(f"{action_str} {qty} shares of {ticker} on {name}.")
                success_count += 1
            else:
                print(f"Failed to {side} {ticker}: {response.get('Message')}")
                failure_count += 1
        except Exception as e:
            print(f"Error trading {ticker} on {name}: {str(e)}")
            traceback.print_exc()
            failure_count += 1

        return success_count > 0

    async def validate(side, qty, ticker, price):
        """Validate order via broker dry-run.

        Returns:
            (True, ""): Order is valid
            (False, reason): Order would fail
            (None, ""): No credentials
        """
        await rate_limiter.wait_if_needed(name)

        from brokers.session_manager import session_manager

        broker = await session_manager.get_session(name)
        if not broker:
            return (None, "")

        try:
            account_info = await asyncio.to_thread(broker.get_account_info)
            account_number = account_info.get("Data", {}).get("accountNumber")
            if not account_number:
                return (False, "No account found")

            if side == "buy":
                def _validate_and_dry_run():
                    v = broker.validate_buy(ticker, qty, 1, account_number)
                    return broker.execute_buy(ticker, qty, account_number,
                        dry_run=True, validation_response=v)
                response = await asyncio.to_thread(_validate_and_dry_run)
            else:
                holdings_response = await asyncio.to_thread(
                    broker.check_stock_holdings, ticker, account_number
                )
                available_qty = holdings_response.get("Data", {}).get("enableAmount", 0)
                if int(available_qty) < qty:
                    return (False, f"Insufficient shares ({available_qty} available)")
                validation = await asyncio.to_thread(
                    broker.validate_sell, ticker, qty, account_number
                )
                if validation.get("Outcome") != "Success":
                    return (False, validation.get("Message", "Sell validation failed")[:100])
                return (True, "")

            if response.get("Outcome") == "Success":
                return (True, "")
            return (False, response.get("Message", "Validation failed")[:100])
        except Exception as e:
            return (False, str(e).split("\n")[0][:100])

    async def get_holdings(ticker=None):
        """Get holdings from the broker."""
        await rate_limiter.wait_if_needed(name)

        from brokers.session_manager import session_manager

        broker = await session_manager.get_session(name)
        if not broker:
            print(f"No {name} credentials supplied, skipping")
            return None

        return await _get_broker_holdings(broker, name, ticker)

    async def get_session(session_manager):
        """Get or create the broker session."""
        if session_key not in session_manager._initialized:
            user = os.getenv(env_user)
            password = os.getenv(env_pass)

            if not (user and password):
                session_manager.sessions[session_key] = None
                session_manager._initialized.add(session_key)
                return None

            try:
                broker = await asyncio.to_thread(
                    api_class, user, password, creds_path="./tokens/"
                )
                if await _login_broker(broker, name):
                    session_manager.sessions[session_key] = broker
                    print(f"✓ {name} session initialized")
                else:
                    session_manager.sessions[session_key] = None
            except Exception as e:
                print(f"✗ Failed to initialize {name} session: {e}")
                session_manager.sessions[session_key] = None

            session_manager._initialized.add(session_key)

        return session_manager.sessions.get(session_key)

    # Give the closures broker-specific names for clearer tracebacks/repr.
    lower = name.lower()
    trade.__name__ = trade.__qualname__ = f"{lower}Trade"
    validate.__name__ = validate.__qualname__ = f"{lower}Validate"
    get_holdings.__name__ = get_holdings.__qualname__ = f"{lower}GetHoldings"
    get_session.__name__ = get_session.__qualname__ = f"get_{lower}_session"

    return trade, validate, get_holdings, get_session
