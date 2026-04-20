"""Webull broker integration using webull library.

IMPORTANT: Webull's API login is broken as of September 2025 (403 errors).

All credentials are configured via WEBULL_PROFILES env var (JSON).
Run `./stockshotgun setup` to configure profiles interactively.

See: https://github.com/tedchou12/webull/issues/456
"""

import asyncio
import hashlib
import os
import traceback
import importlib
import json
from .base import rate_limiter


def _normalize_account_ids(raw_value):
    if raw_value is None:
        return []

    values = []
    if isinstance(raw_value, list):
        source = raw_value
    else:
        source = [raw_value]

    for item in source:
        for token in str(item).split(","):
            text = token.strip()
            if text:
                values.append(text)

    deduped = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _profile_from_raw(raw_profile, default_name):
    if not isinstance(raw_profile, dict):
        return None

    account_source = raw_profile.get("account_ids")
    if account_source is None:
        account_source = raw_profile.get("account_id")

    profile = {
        "name": str(raw_profile.get("name") or default_name),
        "access_token": str(raw_profile.get("access_token") or "").strip(),
        "refresh_token": str(raw_profile.get("refresh_token") or "").strip(),
        "uuid": str(raw_profile.get("uuid") or "").strip(),
        "account_ids": _normalize_account_ids(account_source),
        "device_id": str(
            raw_profile.get("device_id") or raw_profile.get("did") or ""
        ).strip(),
        "username": str(
            raw_profile.get("username") or raw_profile.get("user") or ""
        ).strip(),
        "password": str(
            raw_profile.get("password") or raw_profile.get("pass") or ""
        ).strip(),
        "trading_pin": str(raw_profile.get("trading_pin") or "").strip(),
    }
    return profile


def _load_webull_profiles_from_env():
    raw_profiles = os.getenv("WEBULL_PROFILES")
    if not raw_profiles:
        return []

    try:
        parsed = json.loads(raw_profiles)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    if isinstance(parsed, dict):
        parsed = parsed.get("profiles", [])

    profiles = []
    if isinstance(parsed, list):
        for idx, raw_profile in enumerate(parsed):
            normalized = _profile_from_raw(raw_profile, f"profile-{idx + 1}")
            if normalized:
                profiles.append(normalized)
    return profiles


def _has_valid_trade_token(profiles):
    """Check if any profile has a valid trade token."""
    for profile in profiles:
        client = profile.get("client")
        if client:
            trade_token = str(getattr(client, "_trade_token", "") or "").strip()
            if trade_token:
                return True
    return False


def _session_profiles(webull_session):
    profiles = (
        webull_session.get("profiles") if isinstance(webull_session, dict) else None
    )
    if profiles:
        return profiles

    if isinstance(webull_session, dict) and webull_session.get("client"):
        return [
            {
                "name": "default",
                "client": webull_session["client"],
                "accounts": webull_session.get("accounts", []),
            }
        ]
    return []


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


def _extract_quote_price(quote):
    """Extract the best available limit price from a Webull quote dict.

    Prefers ask price (for buys), then falls back to close/open.
    Returns a string price, or None if no usable price found.
    """
    if not quote:
        return None
    for field in ("ask", "close", "open", "pPrice"):
        val = quote.get(field)
        if val:
            try:
                f = float(val)
                if f > 0:
                    return str(f)
            except (ValueError, TypeError):
                pass
    ask_list = quote.get("askList")
    if ask_list and isinstance(ask_list, list):
        first = ask_list[0]
        if isinstance(first, dict):
            val = first.get("price")
            if val:
                try:
                    f = float(val)
                    if f > 0:
                        return str(f)
                except (ValueError, TypeError):
                    pass
    return None


async def webullValidate(side, qty, ticker, price):
    """Validate order via Webull ticker lookup.

    Returns:
        (True, ""): Ticker is valid and tradeable
        (False, reason): Ticker not found
        (None, ""): No credentials
    """
    await rate_limiter.wait_if_needed("Webull")

    from .session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        return (None, "")

    profiles = _session_profiles(webull_session)
    if not profiles:
        return (None, "")

    first_error = ""
    for profile in profiles:
        wb = profile["client"]
        try:
            ticker_id = await asyncio.to_thread(wb.get_ticker, ticker)
            if ticker_id:
                quote = await asyncio.to_thread(wb.get_quote, ticker.upper())
                trade_status = str(quote.get("tradeStatus", "")).upper()
                if trade_status in {"D", "S", "H", "P"}:
                    return (
                        False,
                        f"Ticker is not currently tradeable on Webull (tradeStatus={trade_status})",
                    )

                exchange_code = str(quote.get("exchangeCode", "")).upper()
                is_market_order = not price or str(price).lower() == "market"
                _OTC_PREFIXES = ("OTC", "PNK", "PINX", "GREY")
                if is_market_order and exchange_code.startswith(_OTC_PREFIXES):
                    if _extract_quote_price(quote):
                        return (True, "")  # webullTrade will auto-convert to limit at current price
                    return (
                        False,
                        f"Webull OTC/Pink Sheet trades require a limit price (exchangeCode={exchange_code}); rerun with --price",
                    )

                return (True, "")
        except Exception as e:
            if not first_error:
                first_error = str(e).split("\n")[0][:100]

    return (False, first_error or "Ticker not found")


def _check_result_allows_order(check_result):
    if not isinstance(check_result, dict):
        return (False, "Malformed order-check response")

    forward_flag = check_result.get("forward")
    if forward_flag is False:
        check_list = check_result.get("checkResultList") or []
        if check_list and isinstance(check_list, list):
            first = check_list[0] if isinstance(check_list[0], dict) else {}
            code = first.get("code", "")
            msg = first.get("msg", "Order blocked by Webull")
            return (False, f"{code}: {msg}" if code else str(msg))
        return (False, str(check_result.get("msg") or "Order blocked by Webull"))

    if forward_flag is None and check_result.get("success") is False:
        return (False, str(check_result.get("msg") or "Order check failed"))

    return (True, "")


def _raw_webull_place_order(wb, account_id, ticker, action, order_type, qty, price):
    requests = importlib.import_module("requests")

    ticker_id = wb.get_ticker(ticker.upper())
    if not ticker_id:
        return {"success": False, "msg": f"Ticker not found: {ticker}"}

    quantity = float(qty) if order_type == "MKT" else int(float(qty))
    order_data = {
        "action": action,
        "comboType": "NORMAL",
        "orderType": order_type,
        "outsideRegularTradingHour": False if order_type == "MKT" else True,
        "quantity": quantity,
        "serialId": __import__("uuid").uuid4().hex,
        "tickerId": ticker_id,
        "timeInForce": "GTC",
    }
    if order_type == "LMT":
        order_data["lmtPrice"] = float(price)

    headers = wb.build_req_headers(include_trade_token=True, include_time=True)

    check_resp = requests.post(
        wb._urls.check_stock_order(account_id),
        json=order_data,
        headers=headers,
        timeout=wb.timeout,
    )
    check_result = check_resp.json() if check_resp.content else {}

    allowed, reason = _check_result_allows_order(check_result)
    if not allowed:
        return {"success": False, "msg": reason}

    place_resp = requests.post(
        wb._urls.place_orders(account_id),
        json=order_data,
        headers=headers,
        timeout=wb.timeout,
    )

    if not place_resp.content:
        return {
            "success": 200 <= place_resp.status_code < 300,
            "msg": f"HTTP {place_resp.status_code}",
        }

    try:
        payload = place_resp.json()
    except ValueError:
        return {
            "success": False,
            "msg": f"Invalid JSON from order place: HTTP {place_resp.status_code}",
        }

    if isinstance(payload, dict):
        return payload

    return {"success": False, "msg": "Unexpected order response payload"}


async def _place_webull_order_with_fallback(
    wb, account_id, ticker, action, order_type, qty, price
):
    try:
        if order_type == "MKT":
            return await asyncio.to_thread(
                wb.place_order,
                stock=ticker.upper(),
                action=action,
                orderType=order_type,
                quant=qty,
            )

        return await asyncio.to_thread(
            wb.place_order,
            stock=ticker.upper(),
            action=action,
            orderType=order_type,
            price=float(price),
            quant=qty,
        )
    except KeyError as ke:
        missing_key = str(ke).strip("'\"")
        if missing_key != "forward":
            raise

        return await asyncio.to_thread(
            _raw_webull_place_order,
            wb,
            account_id,
            ticker,
            action,
            order_type,
            qty,
            price,
        )


_WEBULL_NEW_DEVICE_PHRASE = "log in on a new device"

# Webull tiered minimum order size restrictions (reactive — detected from API error codes)
_WEBULL_MIN_ORDER_TIERS = [
    ("CANT_TRADE_FOR_PRICE_BETWEEN_001_AND_0099", 1000),   # $0.01–$0.099: min 1000 shares
    ("CANT_TRADE_FOR_PRICE_BETWEEN_0099_AND_0999", 100),   # $0.10–$0.999: min 100 shares
]


async def _place_order_with_min_adjustment(wb, account_id, ticker, action, order_type, qty, price):
    """Place a Webull order, automatically handling tiered minimum share requirements.

    Webull enforces minimum order sizes for low-priced stocks:
      - $0.01–$0.099: minimum 1000 shares
      - $0.10–$0.999: minimum 100 shares

    When the minimum fires on a BUY, we buy the minimum quantity and sell the excess
    to net the originally requested quantity.

    Returns:
        (response, excess_qty): response dict from place_order, and how many excess shares
        were bought (0 unless the minimum-order workaround fired).
    Raises:
        Exception: any error other than a handled minimum-order restriction.
    """
    try:
        response = await _place_webull_order_with_fallback(
            wb, account_id, ticker, action, order_type, qty, price
        )
        return response, 0
    except Exception as e:
        if action != "BUY":
            raise

        err_str = str(e)
        min_shares = None
        for error_code, minimum in _WEBULL_MIN_ORDER_TIERS:
            if error_code in err_str:
                min_shares = minimum
                break

        if min_shares is None:
            raise

        requested = int(float(qty))
        if requested >= min_shares:
            raise  # Already at minimum — different issue

        excess_qty = min_shares - requested
        print(
            f"[Webull] {ticker} requires {min_shares} share minimum; "
            f"buying {min_shares}, will sell {excess_qty} excess to net {requested} shares"
        )
        await rate_limiter.wait_if_needed("Webull")
        response = await _place_webull_order_with_fallback(
            wb, account_id, ticker, action, order_type, min_shares, price
        )
        return response, excess_qty


async def webullTrade(side, qty, ticker, price):
    """Execute a trade on Webull.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials supplied
    """
    await rate_limiter.wait_if_needed("Webull")

    from .session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        print("No Webull credentials supplied, skipping")
        return None

    profiles = _session_profiles(webull_session)

    # Auto-trigger browser re-auth if session has no valid profiles or trade token
    if not profiles or not _has_valid_trade_token(profiles):
        print("Webull session expired or missing trade token, re-authenticating via browser...")
        webull_session = await reauth_webull_session(session_manager)
        if not webull_session:
            print("Webull browser re-authentication failed, skipping")
            return None
        profiles = _session_profiles(webull_session)
        if not profiles:
            print("No Webull profiles after re-authentication, skipping")
            return None

    success_count = 0
    failure_count = 0

    # Map side to action
    action = "BUY" if side.lower() == "buy" else "SELL"

    # For market orders, try to auto-derive a limit price.
    # Webull requires limit orders for OTC/Pink Sheet stocks; auto-conversion
    # avoids the "Inner server error" that market orders produce on those stocks.
    effective_price = price
    if not price and profiles:
        try:
            await rate_limiter.wait_if_needed("Webull")
            quote = await asyncio.to_thread(profiles[0]["client"].get_quote, ticker.upper())
            derived = _extract_quote_price(quote)
            if derived:
                effective_price = derived
                print(f"[Webull] Market order auto-converted to limit at ${float(derived):.4f} (ask/close)")
        except Exception:
            pass  # Fall through to market order

    order_type = "LMT" if effective_price else "MKT"

    for profile in profiles:
        wb = profile["client"]
        profile_name = profile.get("name", "default")
        accounts = profile.get("accounts", [])

        trade_token = getattr(wb, "_trade_token", "")
        if not str(trade_token).strip():
            print(
                f"Skipping Webull profile {profile_name}: no trade token. Add trading_pin to the profile in WEBULL_PROFILES."
            )
            failure_count += max(1, len(accounts))
            continue

        for account in accounts:
            account_id = account["account_id"]
            try:
                await asyncio.to_thread(wb.set_account_id, account_id)
                await rate_limiter.wait_if_needed("Webull")

                try:
                    response, excess_qty = await _place_order_with_min_adjustment(
                        wb, account_id, ticker, action, order_type, qty, effective_price
                    )
                except KeyError as ke:
                    print(
                        f"⚠ Webull API error for {ticker} on profile {profile_name} account {account_id}"
                    )
                    print(
                        "  The stock might not be tradeable, or there's an issue with the order"
                    )
                    print(f"  Library error: Missing key '{ke}'")
                    failure_count += 1
                    continue
                except Exception as order_error:
                    err_str = str(order_error)
                    if _WEBULL_NEW_DEVICE_PHRASE in err_str:
                        print(
                            f"[Webull] Device not authorized for profile {profile_name}. "
                            f"Ensure WEBULL_DID is set in .env (capture it from the Chrome extension). "
                            f"Skipping remaining accounts for this profile."
                        )
                        failure_count += len(accounts)
                        break  # skip remaining accounts — same device issue
                    print(
                        f"Error placing order for {ticker} on Webull profile {profile_name} account {account_id}: {err_str}"
                    )
                    traceback.print_exc()
                    failure_count += 1
                    continue

                if response and response.get("success"):
                    action_str = "Bought" if side.lower() == "buy" else "Sold"
                    if not price and effective_price:
                        order_type_str = f"auto-limit @ ${float(effective_price):.4f}"
                    elif effective_price:
                        order_type_str = f"limit @ ${effective_price}"
                    else:
                        order_type_str = "market"
                    print(
                        f"{action_str} {qty} shares of {ticker} on Webull profile {profile_name} account {account_id} ({order_type_str})"
                    )
                    success_count += 1

                    if excess_qty > 0:
                        try:
                            sell_resp = await _place_webull_order_with_fallback(
                                wb, account_id, ticker, "SELL", "LMT", excess_qty, effective_price
                            )
                            if sell_resp and sell_resp.get("success"):
                                print(
                                    f"  ↩ Sold {excess_qty} excess {ticker} shares on {profile_name} account {account_id}"
                                )
                            else:
                                sell_err = (sell_resp.get("msg", "Unknown error") if sell_resp else "No response")
                                print(
                                    f"  ⚠ EXCESS SELL FAILED for {ticker} on {profile_name} account {account_id}: {sell_err}"
                                )
                                print(f"    ⚠ Manual action required: sell {excess_qty} shares of {ticker}")
                        except Exception as sell_e:
                            print(
                                f"  ⚠ EXCESS SELL ERROR for {ticker} on {profile_name} account {account_id}: {sell_e}"
                            )
                            print(f"    ⚠ Manual action required: sell {excess_qty} shares of {ticker}")
                else:
                    error_msg = (
                        response.get("msg", "Unknown error")
                        if response
                        else "No response"
                    )
                    if _WEBULL_NEW_DEVICE_PHRASE in error_msg:
                        print(
                            f"[Webull] Device not authorized for profile {profile_name}. "
                            f"Ensure WEBULL_DID is set in .env (capture it from the Chrome extension). "
                            f"Skipping remaining accounts for this profile."
                        )
                        failure_count += len(accounts)
                        break  # skip remaining accounts — same device issue
                    print(
                        f"Failed to place order for {ticker} on Webull profile {profile_name} account {account_id}: {error_msg}"
                    )
                    failure_count += 1

            except Exception as e:
                print(
                    f"Error placing order for {ticker} on Webull profile {profile_name} account {account_id}: {str(e)}"
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

    from .session_manager import session_manager

    webull_session = await session_manager.get_session("Webull")
    if not webull_session:
        print("No Webull credentials supplied, skipping")
        return None

    profiles = _session_profiles(webull_session)

    # Auto-trigger browser re-auth if session has no valid profiles or trade token
    if not profiles or not _has_valid_trade_token(profiles):
        print("Webull session expired or missing trade token, re-authenticating via browser...")
        webull_session = await reauth_webull_session(session_manager)
        if not webull_session:
            print("Webull browser re-authentication failed, skipping")
            return None
        profiles = _session_profiles(webull_session)
        if not profiles:
            print("No Webull profiles after re-authentication, skipping")
            return None

    try:
        holdings_data = {}

        multiple_profiles = len(profiles) > 1
        for profile in profiles:
            wb = profile["client"]
            profile_name = profile.get("name", "default")
            accounts = profile.get("accounts", [])

            for account in accounts:
                account_id = account["account_id"]
                account_key = (
                    f"{profile_name}:{account_id}" if multiple_profiles else account_id
                )

                try:
                    await asyncio.to_thread(wb.set_account_id, account_id)

                    positions = None

                    try:
                        positions = await asyncio.to_thread(wb.get_positions)
                    except (KeyError, AttributeError, TypeError) as e:
                        try:
                            positions = await asyncio.to_thread(
                                wb.get_positions, v2=True
                            )
                        except Exception as v2_error:
                            print(
                                f"⚠ Both v1 and v2 get_positions failed for profile {profile_name} account {account_id}"
                            )
                            print(f"  v1 error: {type(e).__name__}: {e}")
                            print(f"  v2 error: {type(v2_error).__name__}: {v2_error}")
                            positions = None

                    if not positions:
                        holdings_data[account_key] = []
                        continue

                    formatted_positions = []

                    for position in positions:
                        parsed = _parse_webull_position(position, ticker)
                        if parsed:
                            formatted_positions.append(parsed)

                    holdings_data[account_key] = formatted_positions

                except Exception as e:
                    print(
                        f"Error getting holdings for Webull profile {profile_name} account {account_id}: {str(e)}"
                    )
                    traceback.print_exc()
                    holdings_data[account_key] = []

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting Webull holdings: {str(e)}")
        traceback.print_exc()
        return None


def _try_trade_token(wb, trading_pin):
    """Attempt to obtain a trade token with diagnostic response capture.

    Replicates the webull library's get_trade_token logic but captures
    the full API response for diagnostics on failure.

    Returns (success: bool, response_body: dict | None).
    """
    _requests = importlib.import_module("requests")

    headers = wb.build_req_headers()
    salted = ("wl_app-a&b@!423^" + trading_pin).encode("utf-8")
    data = {"pwd": hashlib.md5(salted).hexdigest()}
    url = wb._urls.trade_token()
    try:
        resp = _requests.post(url, json=data, headers=headers, timeout=wb.timeout)
        result = resp.json()
    except Exception:
        return False, None
    if "tradeToken" in result:
        wb._trade_token = result["tradeToken"]
        return True, result
    return False, result


async def _obtain_trade_token(wb, trading_pin, profile_name):
    """Obtain a trade token, retrying once after a token refresh on failure."""
    ok, body = await asyncio.to_thread(_try_trade_token, wb, trading_pin)
    if ok:
        return True

    print(
        f"[Webull] Trade token request failed for profile {profile_name}, "
        f"attempting token refresh..."
    )
    if body:
        print(f"[Webull] Trade token API response: {body}")

    try:
        refresh_result = await asyncio.to_thread(wb.refresh_login)
        if refresh_result.get("accessToken"):
            print(f"[Webull] Token refresh succeeded for profile {profile_name}, retrying trade token...")
            ok2, body2 = await asyncio.to_thread(_try_trade_token, wb, trading_pin)
            if ok2:
                return True
            print(
                f"⚠ Trade token still failed after refresh for profile {profile_name}"
            )
            if body2:
                print(f"[Webull] Trade token API response (post-refresh): {body2}")
        else:
            print(
                f"⚠ Token refresh did not return new access token for profile {profile_name}: {refresh_result}"
            )
    except Exception as refresh_err:
        print(
            f"⚠ Token refresh failed for profile {profile_name}: {refresh_err}"
        )

    return False


async def get_webull_session(session_manager):
    if "webull" in session_manager._initialized:
        return session_manager.sessions.get("webull")

    profiles = _load_webull_profiles_from_env()
    if not profiles:
        session_manager.sessions["webull"] = None
        session_manager._initialized.add("webull")
        return None

    initialized_profiles = []

    try:
        webull_module = importlib.import_module("webull")
        webull_factory = getattr(webull_module, "webull")

        for raw_profile in profiles:
            profile_name = raw_profile.get("name", "default")
            access_token = raw_profile.get("access_token", "")
            refresh_token = raw_profile.get("refresh_token", "")
            uuid = raw_profile.get("uuid", "")
            account_ids = list(raw_profile.get("account_ids", []))
            device_id = raw_profile.get("device_id", "")
            username = raw_profile.get("username", "")
            password = raw_profile.get("password", "")
            trading_pin = raw_profile.get("trading_pin", "")

            has_token_creds = all([access_token, refresh_token, uuid])
            has_login_creds = all([username, password])

            if not has_token_creds and not has_login_creds:
                print(f"⚠ Skipping Webull profile {profile_name}: missing credentials")
                continue

            wb = webull_factory()

            if device_id:
                did_setter = getattr(wb, "set_did", None)
                if did_setter is None:
                    did_setter = getattr(wb, "_set_did", None)
                if did_setter is not None:
                    await asyncio.to_thread(did_setter, device_id)
                elif hasattr(wb, "_did"):
                    wb._did = device_id

            if not has_token_creds and has_login_creds:
                try:
                    from setup import _capture_webull_tokens_with_zendriver  # type: ignore[import-untyped]

                    print(
                        f"Attempting Zendriver Webull token capture for profile {profile_name}..."
                    )
                    captured = (
                        await _capture_webull_tokens_with_zendriver(username, password)
                        or {}
                    )

                    captured_access = str(captured.get("access_token", "")).strip()
                    captured_refresh = str(captured.get("refresh_token", "")).strip()
                    captured_uuid = str(captured.get("uuid", "")).strip()
                    captured_did = str(captured.get("device_id", "")).strip()
                    captured_accounts = _normalize_account_ids(
                        captured.get("account_ids") or []
                    )

                    if captured_access and captured_refresh and captured_uuid:
                        access_token = captured_access
                        refresh_token = captured_refresh
                        uuid = captured_uuid
                        has_token_creds = True

                        if captured_did:
                            device_id = captured_did
                            did_setter = getattr(wb, "set_did", None)
                            if did_setter is None:
                                did_setter = getattr(wb, "_set_did", None)
                            if did_setter is not None:
                                await asyncio.to_thread(did_setter, device_id)
                            elif hasattr(wb, "_did"):
                                wb._did = device_id

                        if captured_accounts and not account_ids:
                            account_ids = captured_accounts

                        print(
                            f"✓ Zendriver capture produced Webull token credentials for profile {profile_name}"
                        )
                    else:
                        print(
                            f"⚠ Zendriver capture did not return complete token credentials for profile {profile_name}"
                        )
                except Exception as capture_error:
                    print(
                        f"⚠ Zendriver token capture failed for profile {profile_name}: {capture_error}"
                    )

            if has_token_creds:
                print(
                    f"Using pre-obtained Webull credentials (api_login) for profile {profile_name}..."
                )
                await asyncio.to_thread(
                    wb.api_login,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expire="2099-01-01T00:00:00.000+0000",
                    uuid=uuid,
                )
                if account_ids:
                    wb._account_id = account_ids[0]
            else:
                print(
                    f"⚠ Using traditional login for Webull profile {profile_name} (likely to fail due to Webull API changes)"
                )
                await asyncio.to_thread(wb.login, username, password)

            if trading_pin:
                if not device_id:
                    print(
                        f"⚠ No WEBULL_DID set for profile {profile_name} — "
                        f"trade token may fail without a valid device ID"
                    )
                try:
                    trade_token_ok = await _obtain_trade_token(
                        wb, trading_pin, profile_name
                    )
                    if not trade_token_ok:
                        print(
                            f"⚠ WEBULL_TRADING_PIN was provided but trade token retrieval failed for profile {profile_name}. "
                            f"Trading will be disabled for this profile."
                        )
                except Exception as pin_error:
                    print(
                        f"⚠ Failed to set Webull trade token for profile {profile_name}: {pin_error}"
                    )

            accounts = []
            if account_ids:
                print(
                    f"Using {len(account_ids)} provided account ID(s) for profile {profile_name}"
                )
                for idx, aid in enumerate(account_ids):
                    accounts.append({"account_id": aid, "index": idx})

                print(
                    f"Attempting to discover additional accounts for profile {profile_name}..."
                )
                existing_ids = [acc["account_id"] for acc in accounts]
                discovered = await _discover_accounts(
                    wb,
                    start_index=0,
                    existing_account_ids=existing_ids,
                )

                for account in discovered:
                    account["index"] = len(accounts)
                    accounts.append(account)
                    print(
                        f"  + Discovered additional account for {profile_name}: {account['account_id']}"
                    )
            else:
                accounts = await _discover_accounts(wb)

            if not accounts:
                print(f"⚠ No Webull accounts found for profile {profile_name}")
                continue

            initialized_profiles.append(
                {
                    "name": profile_name,
                    "client": wb,
                    "accounts": accounts,
                }
            )

        if not initialized_profiles:
            raise Exception("No Webull profiles could be initialized")

        merged_accounts = []
        for profile in initialized_profiles:
            merged_accounts.extend(profile["accounts"])

        session_manager.sessions["webull"] = {
            "profiles": initialized_profiles,
            "client": initialized_profiles[0]["client"],
            "accounts": merged_accounts,
        }

        total_accounts = sum(
            len(profile["accounts"]) for profile in initialized_profiles
        )
        print(
            f"✓ Webull session initialized ({len(initialized_profiles)} profile(s), {total_accounts} account(s))"
        )

    except Exception as e:
        print(f"✗ Failed to initialize Webull session: {e}")
        traceback.print_exc()
        session_manager.sessions["webull"] = None

    session_manager._initialized.add("webull")
    return session_manager.sessions.get("webull")


async def reauth_webull_session(session_manager):
    """Re-authenticate Webull via Zendriver browser capture when tokens are expired.

    Opens a browser to capture fresh tokens, then re-initializes the session
    with trade token support.
    """
    from setup import _capture_webull_tokens_with_zendriver  # type: ignore[import-untyped]

    profiles = _load_webull_profiles_from_env()
    if not profiles:
        print("⚠ No Webull profiles configured")
        return None

    profile = profiles[0]
    username = profile.get("username", "")
    password = profile.get("password", "")
    trading_pin = profile.get("trading_pin", "")

    print(f"Opening browser for Webull re-authentication ({profile['name']})...")
    captured = await _capture_webull_tokens_with_zendriver(username, password) or {}

    access_token = str(captured.get("access_token", "")).strip()
    refresh_token = str(captured.get("refresh_token", "")).strip()
    uuid = str(captured.get("uuid", "")).strip()
    device_id = str(captured.get("device_id", "")).strip()
    captured_accounts = _normalize_account_ids(captured.get("account_ids") or [])

    print(
        f"[Webull reauth] Captured: access_token={'yes' if access_token else 'NO'}, "
        f"refresh_token={'yes' if refresh_token else 'NO'}, "
        f"uuid={'yes' if uuid else 'NO'}, "
        f"device_id={'yes' if device_id else 'NO'}, "
        f"accounts={len(captured_accounts)}"
    )

    if not all([access_token, refresh_token, uuid]):
        print("⚠ Zendriver capture did not return complete token credentials")
        return None

    webull_module = importlib.import_module("webull")
    webull_factory = getattr(webull_module, "webull")
    wb = webull_factory()

    if device_id:
        did_setter = getattr(wb, "set_did", None) or getattr(wb, "_set_did", None)
        if did_setter is not None:
            await asyncio.to_thread(did_setter, device_id)
        elif hasattr(wb, "_did"):
            wb._did = device_id

    await asyncio.to_thread(
        wb.api_login,
        access_token=access_token,
        refresh_token=refresh_token,
        token_expire="2099-01-01T00:00:00.000+0000",
        uuid=uuid,
    )

    account_ids = captured_accounts or _normalize_account_ids(profile.get("account_ids") or [])
    if account_ids:
        wb._account_id = account_ids[0]

    # Obtain trade token
    trade_ok = False
    if trading_pin:
        trade_ok = await _obtain_trade_token(wb, trading_pin, profile["name"])

    if not trade_ok:
        print(f"⚠ Webull re-auth: session created but trade token failed")
        return None

    # Discover accounts
    accounts = []
    if account_ids:
        for idx, aid in enumerate(account_ids):
            accounts.append({"account_id": aid, "index": idx})
        existing_ids = [acc["account_id"] for acc in accounts]
        discovered = await _discover_accounts(wb, start_index=0, existing_account_ids=existing_ids)
        for account in discovered:
            account["index"] = len(accounts)
            accounts.append(account)
    else:
        accounts = await _discover_accounts(wb)

    if not accounts:
        print(f"⚠ Webull re-auth: no accounts found")
        return None

    new_profile = {
        "name": profile["name"],
        "client": wb,
        "accounts": accounts,
    }

    session_manager.sessions["webull"] = {
        "profiles": [new_profile],
        "client": wb,
        "accounts": accounts,
    }

    print(f"✓ Webull re-authenticated ({len(accounts)} account(s), trade token active)")
    return session_manager.sessions["webull"]
