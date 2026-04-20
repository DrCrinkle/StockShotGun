"""SoFi broker integration."""

import os
import asyncio
import pyotp
import traceback
from .browser_utils import (
    create_browser,
    stop_browser,
    get_page_url,
    wait_for_ready_state,
)
from curl_cffi import requests as curl_requests
from .base import rate_limiter


def _build_sofi_headers(csrf_token=None):
    """Build headers for SoFi API requests."""
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
        "x-requested-with": "XMLHttpRequest",
    }
    if csrf_token:
        headers["csrf-token"] = csrf_token
        headers["origin"] = "https://www.sofi.com"
        headers["referer"] = "https://www.sofi.com/"
        headers["sec-fetch-site"] = "same-origin"
        headers["sec-fetch-mode"] = "cors"
        headers["sec-fetch-dest"] = "empty"
    return headers


def _parse_js_cookies(cookie_string):
    """Parse document.cookie string into a dict."""
    cookies = {}
    if not cookie_string:
        return cookies
    for pair in cookie_string.split(";"):
        pair = pair.strip()
        if "=" in pair:
            eq_idx = pair.index("=")
            name = pair[:eq_idx].strip()
            value = pair[eq_idx + 1:].strip()
            if name:
                cookies[name] = value
    return cookies


class _CookieCollector:
    """Collects cookies from Set-Cookie response headers via CDP events.

    Chrome throttles all CDP cookie-read calls (storage.getCookies,
    network.getCookies, network.getAllCookies) when the browser isn't
    focused, making post-login extraction impossible. Instead we capture
    Set-Cookie headers in real-time as responses arrive during login.
    """

    def __init__(self):
        self.cookies: dict[str, str] = {}

    def handle_response_extra_info(self, event):
        """CDP ResponseReceivedExtraInfo handler — parses Set-Cookie headers."""
        if not hasattr(event, "headers"):
            return
        headers = event.headers
        if not headers:
            return
        raw = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
        if not raw:
            return
        for line in raw.split("\n"):
            line = line.strip()
            if not line or "=" not in line:
                continue
            cookie_part = line.split(";")[0]
            eq_idx = cookie_part.index("=")
            name = cookie_part[:eq_idx].strip()
            value = cookie_part[eq_idx + 1:].strip()
            if name:
                self.cookies[name] = value


async def _merge_cookies(page, collector):
    """Merge header-intercepted cookies with JS document.cookie.

    Header cookies take precedence since they include httpOnly cookies.
    """
    js_cookies = await page.evaluate("document.cookie")
    js_parsed = _parse_js_cookies(js_cookies)
    return {**js_parsed, **collector.cookies}


async def _sofi_authenticate(session_info):
    """Authenticate with SoFi using browser automation and return cookies.

    Cookies are captured in real-time from Set-Cookie response headers via CDP
    network events, avoiding post-login CDP cookie extraction calls that Chrome
    throttles when the browser window isn't focused.
    """
    import zendriver.cdp.network as cdp_network

    username = session_info["username"]
    password = session_info["password"]
    totp_secret = session_info.get("totp_secret")

    browser = None
    collector = _CookieCollector()

    try:
        headless = os.getenv("HEADLESS", "true").lower() == "true"
        browser = await create_browser(headless=headless)

        # Try to load existing cookies
        try:
            cookies_path = session_info["cookies_path"]
            await browser.cookies.load(cookies_path)
            page = await browser.get("https://www.sofi.com/wealth/app/overview")
            page.add_handler(cdp_network.ResponseReceivedExtraInfo, collector.handle_response_extra_info)
            await wait_for_ready_state(page)

            current_url = await get_page_url(page)
            if current_url and "overview" in current_url:
                return await _merge_cookies(page, collector)
        except Exception:
            pass

        # Fresh login
        page = await browser.get("https://www.sofi.com/wealth/app")
        page.add_handler(cdp_network.ResponseReceivedExtraInfo, collector.handle_response_extra_info)
        await wait_for_ready_state(page)

        # Check if already logged in
        current_url = await get_page_url(page)
        if current_url and "overview" in current_url:
            return await _merge_cookies(page, collector)

        # Enter credentials
        try:
            username_input = await page.select("input[id=username]", timeout=5)
            if username_input:
                await username_input.send_keys(username)
        except asyncio.TimeoutError:
            pass

        try:
            password_input = await page.select("input[type=password]", timeout=5)
            if password_input:
                await password_input.send_keys(password)
        except asyncio.TimeoutError:
            pass

        try:
            login_button = await page.find("Log In", best_match=True, timeout=5)
            if login_button:
                await login_button.click()
        except asyncio.TimeoutError:
            pass

        await wait_for_ready_state(page)

        # Handle 2FA if needed
        twofa_input = None
        current_url = await get_page_url(page)

        if not (current_url and "overview" in current_url):
            try:
                twofa_input = await page.select("input[id=code]", timeout=3)
            except (asyncio.TimeoutError, AttributeError):
                twofa_input = None

            if not twofa_input:
                await asyncio.sleep(2)
                current_url = await get_page_url(page)

                if not (current_url and "overview" in current_url):
                    try:
                        twofa_input = await page.select("input[id=code]", timeout=2)
                    except (asyncio.TimeoutError, AttributeError):
                        twofa_input = None

        if twofa_input:
            try:
                remember = await page.select("input[id=rememberBrowser]", timeout=5)
                if remember:
                    await remember.click()
            except asyncio.TimeoutError:
                pass

            if totp_secret:
                totp = pyotp.TOTP(totp_secret)
                code = totp.now()
                await twofa_input.send_keys(code)
            else:
                print("SoFi 2FA required. Please check your device for the code.")
                from tui.input_handler import tui_async_input  # type: ignore[import-untyped]

                sms_code = await tui_async_input("Enter SoFi 2FA code: ")
                await twofa_input.send_keys(sms_code)

            try:
                verify_button = await page.find("Verify Code", timeout=5)
                if verify_button:
                    await verify_button.click()
            except asyncio.TimeoutError:
                pass

            await wait_for_ready_state(page)

        # Handle second challenge page
        current_url = await get_page_url(page)
        if current_url and "overview" not in current_url:
            if isinstance(current_url, str) and "challenge" in current_url.lower():
                print("SoFi second challenge detected. Please complete it manually.")
                from tui.input_handler import tui_async_input  # type: ignore[import-untyped]

                await tui_async_input(
                    "Press Enter after completing the SoFi challenge..."
                )
                await asyncio.sleep(2)

        return await _merge_cookies(page, collector)

    finally:
        await stop_browser(browser)


async def _sofi_get_cookies(session_info):
    """Get SoFi cookies, authenticating if necessary. Caches in session."""
    cached = session_info.get("_cached_cookies")
    if cached:
        return cached

    cookies = await _sofi_authenticate(session_info)
    if cookies:
        session_info["_cached_cookies"] = cookies
    return cookies


async def _sofi_get_stock_price(symbol):
    """Fetch current stock price for a symbol."""
    try:
        url = f"https://www.sofi.com/wealth/backend/api/v1/tearsheet/quote?symbol={symbol}&productSubtype=BROKERAGE"
        response = await asyncio.to_thread(
            curl_requests.get,
            url,
            impersonate="chrome",
            headers=_build_sofi_headers(),
        )

        if response.status_code == 200:
            data = response.json()
            price = data.get("price")
            if price:
                return round(float(price), 2)

        print(f"Failed to fetch SoFi stock price for {symbol}")
        return None
    except Exception as e:
        print(f"Error fetching SoFi stock price for {symbol}: {e}")
        return None


async def _sofi_get_funded_accounts(cookies):
    """Get list of funded SoFi accounts."""
    try:
        url = (
            "https://www.sofi.com/wealth/backend/api/v1/user/funded-brokerage-accounts"
        )
        response = await asyncio.to_thread(
            curl_requests.get,
            url,
            impersonate="chrome",
            headers=_build_sofi_headers(),
            cookies=cookies,
        )

        if response.status_code == 200:
            return response.json()

        print("Failed to fetch SoFi funded accounts")
        return None
    except Exception as e:
        print(f"Error fetching SoFi funded accounts: {e}")
        return None


async def _sofi_place_order(
    symbol, quantity, limit_price, account_id, order_type, cookies, csrf_token
):
    """Place a limit or market order on SoFi.

    Returns:
        (True, None): Order placed successfully
        (False, error_text): Order failed, error_text contains response body
    """
    try:
        payload = {
            "operation": order_type,
            "quantity": str(quantity),
            "time": "DAY",
            "type": "LIMIT" if limit_price else "MARKET",
            "symbol": symbol,
            "accountId": account_id,
            "tradingSession": "CORE_HOURS",
        }

        if limit_price:
            payload["limitPrice"] = limit_price

        url = "https://www.sofi.com/wealth/backend/api/v1/trade/order"
        response = await asyncio.to_thread(
            curl_requests.post,
            url,
            impersonate="chrome",
            json=payload,
            headers=_build_sofi_headers(csrf_token),
            cookies=cookies,
        )

        if response.status_code == 200:
            result = response.json()
            if result.get("header") == "Your order is placed.":
                return (True, None)

        error_text = response.text
        print(f"Failed to place SoFi order for {symbol}: {error_text}")
        return (False, error_text)
    except Exception as e:
        print(f"Error placing SoFi order for {symbol}: {e}")
        return (False, str(e))


async def sofiValidate(side, qty, ticker, price):
    """Validate order via SoFi price check.

    Returns:
        (True, ""): Ticker is valid and tradeable
        (False, reason): Ticker not found
        (None, ""): No credentials
    """
    await rate_limiter.wait_if_needed("SoFi")

    from .session_manager import session_manager

    session = await session_manager.get_session("SoFi")
    if not session:
        return (None, "")

    try:
        stock_price = await _sofi_get_stock_price(ticker)
        if stock_price is None:
            return (False, "Ticker not found or not tradeable")
        return (True, "")
    except Exception as e:
        return (False, str(e).split("\n")[0][:100])


async def sofiTrade(side, qty, ticker, price):
    """Execute a trade on SoFi.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials (broker skipped)
    """
    await rate_limiter.wait_if_needed("SoFi")

    from .session_manager import session_manager

    session = await session_manager.get_session("SoFi")
    if not session:
        print("No SoFi credentials supplied, skipping")
        return None

    try:
        cookies = await _sofi_get_cookies(session)
        if not cookies:
            print("Failed to authenticate with SoFi")
            return False

        csrf_token = cookies.get("SOFI_CSRF_COOKIE") or cookies.get("SOFI_R_CSRF_TOKEN")
        if not csrf_token:
            print("Failed to get SoFi CSRF token")
            return False

        if not price:
            stock_price = await _sofi_get_stock_price(ticker)
            if not stock_price:
                print(f"Failed to get price for {ticker}")
                return False

            if side == "buy":
                price = round(stock_price + 0.01, 2)
            else:
                price = round(stock_price - 0.01, 2)

        accounts = await _sofi_get_funded_accounts(cookies)
        if not accounts:
            print("No funded SoFi accounts found")
            return False

        order_type = "BUY" if side == "buy" else "SELL"
        success_count = 0
        failure_count = 0

        for account in accounts:
            account_id = account["accountId"]
            account_type = account.get("accountType", "Unknown")

            if side == "buy":
                buying_power = account.get("accountBuyingPower", 0)
                total_cost = price * qty

                if total_cost > buying_power:
                    print(f"Insufficient buying power in SoFi {account_type} account")
                    failure_count += 1
                    continue

            ok, error_text = await _sofi_place_order(
                ticker, qty, price, account_id, order_type, cookies, csrf_token
            )

            if ok:
                action_str = "Bought" if side == "buy" else "Sold"
                print(f"{action_str} {ticker} on SoFi {account_type} account")
                success_count += 1
            else:
                print(f"Failed to {side} {ticker} on SoFi {account_type} account")
                failure_count += 1

                if error_text and "cannot be traded" in error_text:
                    print(
                        f"{ticker} cannot be traded on SoFi, skipping remaining accounts"
                    )
                    break

        return success_count > 0

    except Exception as e:
        print(f"Error during SoFi trade: {e}")
        traceback.print_exc()
        return False


async def sofiGetHoldings(ticker=None):
    """Get holdings from SoFi accounts."""
    await rate_limiter.wait_if_needed("SoFi")

    from .session_manager import session_manager

    session = await session_manager.get_session("SoFi")
    if not session:
        print("No SoFi credentials supplied, skipping")
        return None

    try:
        cookies = await _sofi_get_cookies(session)
        if not cookies:
            print("Failed to authenticate with SoFi")
            return None

        # Get account information (cookies must be dict[str, str] for curl_cffi)
        cookies_str: dict[str, str] = {k: v for k, v in cookies.items() if v is not None}
        accounts_response = await asyncio.to_thread(
            curl_requests.get,
            "https://www.sofi.com/wealth/backend/v1/json/accounts",
            impersonate="chrome",
            headers=_build_sofi_headers(),
            cookies=cookies_str,
        )

        if accounts_response.status_code != 200:
            print(f"Failed to fetch SoFi account information (status {accounts_response.status_code})")
            return None

        accounts_data = accounts_response.json()
        holdings_data = {}

        for account in accounts_data:
            account_id = account["id"]
            account_number = account["apexAccountId"]

            holdings_url = f"https://www.sofi.com/wealth/backend/api/v3/account/{account_id}/holdings?accountDataType=INTERNAL"
            holdings_response = await asyncio.to_thread(
                curl_requests.get,
                holdings_url,
                impersonate="chrome",
                headers=_build_sofi_headers(),
                cookies=cookies_str,
            )

            if holdings_response.status_code != 200:
                continue

            holdings_json = holdings_response.json()
            formatted_positions = []

            for holding in holdings_json.get("holdings", []):
                symbol = holding.get("symbol")

                if not symbol or symbol == "|CASH|":
                    continue

                if ticker and symbol.upper() != ticker.upper():
                    continue

                shares = float(holding.get("shares", 0))
                price = float(holding.get("price", 0))

                cost_basis = float(holding.get("costBasis", 0))
                if cost_basis == 0 and "avgCost" in holding:
                    avg_cost = float(holding.get("avgCost", 0))
                    cost_basis = avg_cost * shares

                formatted_positions.append(
                    {
                        "symbol": symbol,
                        "quantity": shares,
                        "cost_basis": cost_basis,
                        "current_value": price * shares,
                    }
                )

            if formatted_positions:
                holdings_data[account_number] = formatted_positions

        return holdings_data if holdings_data else None

    except Exception as e:
        print(f"Error getting SoFi holdings: {e}")
        traceback.print_exc()
        return None


async def get_sofi_session(session_manager):
    """Get or create SoFi session using browser automation."""
    if "sofi" not in session_manager._initialized:
        SOFI_USER = os.getenv("SOFI_USER")
        SOFI_PASS = os.getenv("SOFI_PASS")
        SOFI_TOTP = os.getenv("SOFI_TOTP")  # Optional TOTP secret

        if not (SOFI_USER and SOFI_PASS):
            session_manager.sessions["sofi"] = None
            session_manager._initialized.add("sofi")
            return None

        try:
            os.makedirs("./tokens/", exist_ok=True)

            sofi_session = {
                "username": SOFI_USER,
                "password": SOFI_PASS,
                "totp_secret": SOFI_TOTP,
                "cookies_path": "./tokens/sofi_cookies.pkl",
            }

            session_manager.sessions["sofi"] = sofi_session
            print("✓ SoFi credentials loaded")
        except Exception as e:
            print(f"✗ Failed to initialize SoFi session: {e}")
            session_manager.sessions["sofi"] = None

        session_manager._initialized.add("sofi")

    return session_manager.sessions.get("sofi")
