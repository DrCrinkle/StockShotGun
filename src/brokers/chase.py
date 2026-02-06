"""Chase Invest broker integration using zendriver."""

import os
import re
import json
import asyncio
import logging
import traceback
from bs4 import BeautifulSoup
from zendriver import Browser
from brokers.base import broker_event

# Chase API endpoints
HOLDINGS_JSON_URL = "https://secure.chase.com/svc/wr/dwm/secure/gateway/investments/servicing/inquiry-maintenance/digital-investment-positions/v2/positions"
ALL_ACCOUNTS_URL = "https://secure.chase.com/svc/wr/dwm/secure/gateway/investments/servicing/inquiry-maintenance/digital-investment-accounts/v1/all-accounts"
DASHBOARD_MODULE_URL = (
    "https://secure.chase.com/svc/rl/accounts/secure/v1/dashboard/module/list"
)
INVESTMENT_OVERVIEW_CACHE_URL = "/svc/rr/accounts/secure/overview/investment/v1/list"
INVESTMENT_OVERVIEW_URL = f"https://secure.chase.com{INVESTMENT_OVERVIEW_CACHE_URL}"
logger = logging.getLogger(__name__)


class ChaseClient:
    """Chase Invest client with browser automation."""

    def __init__(self, username: str, password: str, headless: bool = True):
        """Initialize Chase client with credentials.

        Args:
            username: Chase username
            password: Chase password
            headless: Run browser in headless mode
        """
        self._username = username
        self._password = password
        self._headless = headless

        # State management
        self._browser = None
        self._page = None
        self._accounts = None
        self._is_authenticated = False

    async def __aenter__(self):
        """Async context manager entry. Browser created lazily on first operation."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit. Cleanup browser if exists."""
        if self._browser:
            try:
                await self._browser.stop()
            except Exception as e:
                broker_event(
                    f"Error stopping browser: {e}",
                    level="warning",
                    logger=logger,
                    exc=e,
                )
            finally:
                self._browser = None
                self._page = None
                self._is_authenticated = False

    async def _ensure_authenticated(self):
        """Ensure browser is authenticated. Lazy authentication - only creates browser when needed."""
        if not self._is_authenticated or not self._browser:
            await self._authenticate()

    async def _authenticate(self):
        """Authenticate with Chase using browser automation."""
        try:
            # Start browser
            browser_args = []

            browser_path = os.getenv("BROWSER_PATH", "/usr/bin/helium-browser")

            self._browser = await Browser.create(
                browser_args=browser_args,
                headless=self._headless,
                browser_executable_path=browser_path,
            )

            # Navigate directly to Chase login page
            self._page = await self._browser.get(
                "https://secure.chase.com/web/auth/?fromOrigin=https://secure.chase.com"
            )
            page = self._page
            await asyncio.sleep(2)
            broker_event("Chase login page loaded", logger=logger)

            for attempt in range(20):
                await asyncio.sleep(1)
                try:
                    has_login_inputs = await page.evaluate(
                        "Boolean(document.querySelector('input[type=password], input[name=\"userId\"], #userId-input-field-input'))"
                    )
                    if has_login_inputs:
                        break
                    if attempt % 5 == 0:
                        broker_event(
                            f"Waiting for login form... ({attempt + 1}s)", logger=logger
                        )
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while waiting for Chase login form",
                        exc_info=exc,
                    )

            # Enter username (selector from chaseinvest-api)
            use_js_login = False
            username_input = None
            username_selectors = [
                "#userId-input-field-input",
                "input[name='userId']",
                "input[id*='userId']",
                "input[autocomplete='username']",
                "input[type='text']",
            ]
            for selector in username_selectors:
                try:
                    username_input = await page.select(selector, timeout=5)
                    if username_input:
                        break
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    continue
            if not username_input:
                username_json = json.dumps(self._username)
                password_json = json.dumps(self._password)
                js_script = """
                (() => {
                    const findInput = (selectors) => {
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            if (el) return el;
                        }
                        return null;
                    };
                    const userSelectors = [
                        '#userId-input-field-input',
                        'input[name="userId"]',
                        'input[id*="userId"]',
                        'input[autocomplete="username"]',
                        'input[type="text"]'
                    ];
                    const passSelectors = [
                        '#password-input-field-input',
                        'input[name="password"]',
                        'input[type="password"]',
                        'input[autocomplete="current-password"]'
                    ];
                    const userEl = findInput(userSelectors);
                    const passEl = findInput(passSelectors);
                    if (userEl) {
                        userEl.focus();
                        userEl.value = __USERNAME__;
                        userEl.dispatchEvent(new Event('input', { bubbles: true }));
                        userEl.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    if (passEl) {
                        passEl.focus();
                        passEl.value = __PASSWORD__;
                        passEl.dispatchEvent(new Event('input', { bubbles: true }));
                        passEl.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                    const buttonSelectors = [
                        '#signin-button',
                        'button[type="submit"]',
                        'button[id*="signin"]',
                        'button[id*="sign"]'
                    ];
                    let buttonEl = null;
                    for (const sel of buttonSelectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            buttonEl = el;
                            break;
                        }
                    }
                    if (!buttonEl) {
                        const buttons = Array.from(document.querySelectorAll('button'));
                        buttonEl = buttons.find(btn => {
                            const text = (btn.innerText || '').toLowerCase();
                            return text.includes('sign in') || text.includes('log on');
                        }) || null;
                    }
                    if (buttonEl) buttonEl.click();
                    return {
                        userFound: Boolean(userEl),
                        passFound: Boolean(passEl),
                        buttonFound: Boolean(buttonEl)
                    };
                })()
                """
                js_script = js_script.replace("__USERNAME__", username_json)
                js_script = js_script.replace("__PASSWORD__", password_json)
                js_result = await page.evaluate(js_script)
                if not js_result or not (
                    js_result.get("userFound") and js_result.get("passFound")
                ):
                    raise Exception(
                        "Could not find username/password input on login page"
                    )
                broker_event("Filled login form via JS fallback", logger=logger)
                broker_event(
                    "Login credentials submitted, waiting for authentication...",
                    logger=logger,
                )
                await asyncio.sleep(5)
                use_js_login = True

            if not use_js_login:
                if not username_input:
                    raise Exception("Could not find username input on login page")
                await username_input.click()
                await username_input.clear_input()
                await username_input.send_keys(self._username)
                await asyncio.sleep(0.5)

            # Enter password
            if not use_js_login:
                password_input = None
                password_selectors = [
                    "#password-input-field-input",
                    "input[name='password']",
                    "input[type='password']",
                    "input[autocomplete='current-password']",
                ]
                for selector in password_selectors:
                    try:
                        password_input = await page.select(selector, timeout=5)
                        if password_input:
                            break
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        continue
                if not password_input:
                    raise Exception("Could not find password input on login page")
                await password_input.send_keys(self._password)
                await asyncio.sleep(0.5)

            # Click sign in button
            if not use_js_login:
                signin_button = None
                signin_selectors = [
                    "#signin-button",
                    "button[type='submit']",
                    "button[id*='signin']",
                    "button[id*='sign']",
                ]
                for selector in signin_selectors:
                    try:
                        signin_button = await page.select(selector, timeout=5)
                        if signin_button:
                            break
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        continue
                if not signin_button:
                    try:
                        signin_button = await page.find("Sign in", best_match=True)
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        signin_button = None
                if not signin_button:
                    try:
                        signin_button = await page.find("Log on", best_match=True)
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        signin_button = None
                if not signin_button:
                    raise Exception("Could not find sign-in button on login page")
                await signin_button.click()

                broker_event(
                    "Login credentials submitted, waiting for authentication...",
                    logger=logger,
                )
                await asyncio.sleep(5)

            # Check for 2FA or security verification
            current_url = await page.evaluate("window.location.href")
            page_text = await page.evaluate("document.body?.innerText || ''")

            broker_event(f"Post-login URL: {current_url}", logger=logger)
            broker_event(
                f"Post-login page preview: {page_text[:300]}...", logger=logger
            )

            # Check for 2FA - look at both URL and page content
            needs_verification = (
                "verification" in current_url.lower()
                or "mfa" in current_url.lower()
                or "authenticate" in current_url.lower()
                or "identify" in current_url.lower()
                or "recognizeuser" in current_url.lower()
                or "verify" in page_text.lower()
                or "send" in page_text.lower()
                and "code" in page_text.lower()
                or "confirm" in page_text.lower()
                and "phone" in page_text.lower()
                or "push notification" in page_text.lower()
                or "check your phone" in page_text.lower()
            )

            if needs_verification:
                broker_event(f"\n{'=' * 60}", logger=logger)
                broker_event(
                    "⚠️  Chase verification/confirmation required!",
                    level="warning",
                    logger=logger,
                )
                broker_event(
                    "Please complete verification in the browser window.",
                    level="warning",
                    logger=logger,
                )
                broker_event(
                    "(Approve the push notification on your phone)",
                    level="warning",
                    logger=logger,
                )
                broker_event(
                    "\nWaiting for verification to complete (up to 2 minutes)...",
                    level="warning",
                    logger=logger,
                )
                broker_event(f"{'=' * 60}\n", logger=logger)

                # Poll for verification completion (up to 120 seconds)
                for attempt in range(60):
                    await asyncio.sleep(2)
                    try:
                        current_url = await page.evaluate("window.location.href")
                        page_text = await page.evaluate(
                            "document.body?.innerText || ''"
                        )

                        # Check if we're past verification - look for dashboard content or absence of verification prompts
                        still_verifying = (
                            "recognizeuser" in current_url.lower()
                            or "verification" in current_url.lower()
                            or "authenticate" in current_url.lower()
                            or "check your phone" in page_text.lower()
                            or "push notification" in page_text.lower()
                            or "we sent" in page_text.lower()
                        )

                        if not still_verifying:
                            broker_event(
                                "✓ Verification completed successfully", logger=logger
                            )
                            break

                        if attempt % 5 == 0:
                            broker_event(
                                f"Still waiting for verification... ({attempt * 2}s)",
                                logger=logger,
                            )
                    except Exception as exc:
                        logger.debug(
                            "Ignored transient error while polling Chase verification state",
                            exc_info=exc,
                        )

            # Wait for post-login processing page to complete
            for attempt in range(30):
                await asyncio.sleep(2)
                try:
                    current_url = await page.evaluate("window.location.href")
                    if "processstatus" not in current_url.lower():
                        break
                    if attempt % 5 == 0:
                        broker_event(
                            f"Waiting for post-login processing... ({attempt * 2}s)",
                            logger=logger,
                        )
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while waiting for Chase post-login processing",
                        exc_info=exc,
                    )

            # Navigate to dashboard overview (shows investment account summaries)
            await asyncio.sleep(1)
            broker_event("Navigating to Chase dashboard...", logger=logger)
            await page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/overview"
            )
            await asyncio.sleep(3)

            # Wait for dashboard content to render
            for attempt in range(15):
                await asyncio.sleep(1)
                try:
                    page_text = await page.evaluate("document.body?.innerText || ''")
                    if len(page_text.strip()) > 200:
                        break
                    if attempt % 5 == 0:
                        broker_event(
                            f"Waiting for dashboard content... ({attempt + 1}s)",
                            logger=logger,
                        )
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while waiting for Chase dashboard content",
                        exc_info=exc,
                    )

            # Verify login success
            current_url = await page.evaluate("window.location.href")
            page_title = await page.evaluate("document.title")

            # Check if we're logged in
            if "logon" in current_url.lower() or "signin" in current_url.lower():
                broker_event(
                    "✗ Chase login failed - still on login page",
                    level="error",
                    logger=logger,
                )
                broker_event(
                    f"Current URL: {current_url}", level="warning", logger=logger
                )
                raise Exception("Chase login failed")

            broker_event("✓ Chase authenticated successfully", logger=logger)
            self._page = page
            self._is_authenticated = True

        except Exception as e:
            broker_event(
                f"Error during Chase authentication: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()
            if self._browser:
                await self._browser.stop()
                self._browser = None
            self._is_authenticated = False
            raise

    async def _discover_accounts(self):
        """
        Discover all Chase investment accounts.
        Returns list of dicts with account info: {index, name, number}
        """
        try:
            # Navigate to investments overview
            await self._page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/investments/youInvest/trade"
            )
            await asyncio.sleep(3)

            html = await self._page.get_content()
            soup = BeautifulSoup(html, "html.parser")

            accounts = []

            # Try to find account selector
            # Chase may have account dropdown or list
            account_elements = soup.select("[data-account-id], [data-account-number]")

            if not account_elements:
                # Fallback: look for account numbers in the page
                # Chase accounts are typically 4 digits
                account_pattern = re.compile(r"\b\d{4}\b")
                account_matches = account_pattern.findall(html)

                if account_matches:
                    for idx, acct_num in enumerate(
                        list(set(account_matches))[:5]
                    ):  # Limit to 5 to avoid false matches
                        accounts.append(
                            {
                                "index": idx,
                                "number": acct_num,
                                "name": f"Chase Account {acct_num}",
                            }
                        )
                else:
                    # Last resort: create single default account
                    accounts = [
                        {
                            "index": 0,
                            "number": "",
                            "name": "Default Chase Account",
                        }
                    ]
            else:
                for idx, elem in enumerate(account_elements):
                    account_number = elem.get("data-account-id") or elem.get(
                        "data-account-number", ""
                    )
                    account_name = (
                        elem.get_text(strip=True) or f"Chase Account {account_number}"
                    )

                    accounts.append(
                        {
                            "index": idx,
                            "number": account_number,
                            "name": account_name,
                        }
                    )

            broker_event(f"Found {len(accounts)} Chase account(s)", logger=logger)
            for account in accounts:
                broker_event(
                    f"  - {account['name']} ({account['number']})", logger=logger
                )

            return accounts

        except Exception as e:
            broker_event(
                f"Error discovering accounts: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()
            # Fallback to single account
            return [
                {
                    "index": 0,
                    "number": "",
                    "name": "Default Chase Account",
                }
            ]

    async def _parse_holdings_table(self, html):
        """Parse Chase holdings table HTML."""
        soup = BeautifulSoup(html, "html.parser")
        holdings = []

        # Chase uses various table/card layouts for positions
        rows = soup.select("tr[data-symbol], .position-row, tbody tr")

        for row in rows:
            try:
                # Try to extract symbol
                symbol_elem = (
                    row.select_one("[data-symbol]")
                    or row.select_one(".symbol")
                    or row.select_one("td:first-child")
                )
                if not symbol_elem:
                    continue

                symbol = symbol_elem.get("data-symbol") or symbol_elem.get_text(
                    strip=True
                )
                # Clean symbol
                symbol = re.sub(r"[^A-Z]", "", symbol.upper())

                if not symbol or len(symbol) > 6:
                    continue

                # Get quantity
                quantity_elem = row.select_one(
                    '[class*="quantity"], .shares, td:nth-child(2)'
                )
                quantity = 0.0
                if quantity_elem:
                    quantity_text = (
                        quantity_elem.get_text(strip=True)
                        .replace(",", "")
                        .replace("shares", "")
                        .strip()
                    )
                    try:
                        quantity = float(quantity_text)
                    except (ValueError, TypeError):
                        continue

                # Get current value
                value_elem = row.select_one(
                    '[class*="value"], [class*="market"], td:nth-child(4)'
                )
                current_value = 0.0
                if value_elem:
                    value_text = (
                        value_elem.get_text(strip=True)
                        .replace("$", "")
                        .replace(",", "")
                    )
                    try:
                        current_value = float(value_text)
                    except (ValueError, TypeError):
                        pass

                # Calculate price
                price = current_value / quantity if quantity > 0 else 0.0

                if quantity > 0:
                    holdings.append(
                        {
                            "symbol": symbol,
                            "quantity": quantity,
                            "price": price,
                            "cost_basis": None,
                            "current_value": current_value,
                        }
                    )

            except Exception as e:
                broker_event(
                    f"Error parsing holdings row: {e}",
                    level="error",
                    logger=logger,
                    exc=e,
                )
                continue

        return holdings

    async def _get_account_ids(self):
        """Get all investment account IDs by calling the accounts API."""
        account_ids = []

        self._accounts = None

        try:
            # First navigate to investments page to establish context
            broker_event("Fetching account IDs via API...", logger=logger)
            await self._page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/overview"
            )
            await asyncio.sleep(2)

            for attempt in range(10):
                await asyncio.sleep(1)
                try:
                    page_text = await self._page.evaluate(
                        "document.body?.innerText || ''"
                    )
                    if len(page_text.strip()) > 200:
                        break
                    if attempt % 5 == 0:
                        print(f"Waiting for dashboard content... ({attempt + 1}s)")
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while waiting for Chase account data",
                        exc_info=exc,
                    )

            # Call the accounts API directly from the browser context
            api_response = await self._page.evaluate(f"""
                (async () => {{
                    try {{
                        const response = await fetch('{ALL_ACCOUNTS_URL}', {{
                            method: 'GET',
                            credentials: 'include',
                            headers: {{
                                'Accept': 'application/json, text/plain, */*',
                                'Content-Type': 'application/json',
                                'x-jpmc-csrf-token': 'NONE'
                            }}
                        }});
                        const text = await response.text();
                        if (response.ok) {{
                            try {{
                                return JSON.parse(text);
                            }} catch (e) {{
                                return {{ error: 'parse', text: text.substring(0, 1000) }};
                            }}
                        }} else {{
                            return {{ error: response.status, text: text.substring(0, 500) }};
                        }}
                    }} catch (e) {{
                        return {{ error: e.message }};
                    }}
                }})()
            """)

            broker_event(
                f"Accounts API response: {str(api_response)[:500]}...", logger=logger
            )

            if api_response and "error" not in api_response:
                # Extract account IDs from the response
                # The structure might be: { accounts: [...], accountConnectors: {...} }
                accounts = api_response.get("accounts", [])
                connectors = api_response.get("accountConnectors", {})

                # Regex-based extraction for nested structures
                try:
                    response_text = json.dumps(api_response)
                    regexes = [
                        r'"accountId"\s*:\s*"([^"]+)"',
                        r'"accountConnector"\s*:\s*"([^"]+)"',
                        r'"accountNumber"\s*:\s*"([^"]+)"',
                    ]
                    for regex in regexes:
                        for match in re.findall(regex, response_text):
                            if match not in account_ids:
                                account_ids.append(match)
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while parsing Chase account IDs from API response",
                        exc_info=exc,
                    )

                if connectors:
                    account_ids = list(connectors.keys())
                elif accounts:
                    for acct in accounts:
                        acct_id = acct.get("accountId") or acct.get("accountConnector")
                        if acct_id:
                            account_ids.append(acct_id)

            if account_ids:
                broker_event(f"Found {len(account_ids)} account ID(s)", logger=logger)
                for aid in account_ids[:5]:
                    broker_event(f"  - {aid}", logger=logger)
            else:
                broker_event(
                    "Could not find account IDs from API",
                    level="warning",
                    logger=logger,
                )

            if not account_ids:
                broker_event(
                    "Fetching account IDs via dashboard module list...", logger=logger
                )
                dashboard_response = None

                try:
                    async with self._page.expect_response(
                        DASHBOARD_MODULE_URL
                    ) as response_info:
                        await self._page.reload()
                        await asyncio.wait_for(response_info.value, timeout=10)
                        body_str, _ = await response_info.response_body
                        dashboard_response = json.loads(body_str)
                        broker_event(
                            "Got dashboard module response via network", logger=logger
                        )
                except Exception as e:
                    broker_event(
                        f"Dashboard network capture failed: {e}",
                        level="warning",
                        logger=logger,
                        exc=e,
                    )

                if dashboard_response is None:
                    dashboard_response = await self._page.evaluate(f"""
                        (async () => {{
                            try {{
                                const response = await fetch('{DASHBOARD_MODULE_URL}', {{
                                    method: 'GET',
                                    credentials: 'include',
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'Content-Type': 'application/json',
                                        'x-jpmc-csrf-token': 'NONE',
                                        'x-requested-with': 'XMLHttpRequest',
                                        'x-jpmc-channel': 'id=C30',
                                        'origin': 'https://secure.chase.com',
                                        'referer': 'https://secure.chase.com/web/auth/dashboard'
                                    }}
                                }});
                                const text = await response.text();
                                if (response.ok) {{
                                    try {{
                                        return JSON.parse(text);
                                    }} catch (e) {{
                                        return {{ error: 'parse', text: text.substring(0, 1000) }};
                                    }}
                                }} else {{
                                    return {{ error: response.status, text: text.substring(0, 500) }};
                                }}
                            }} catch (e) {{
                                return {{ error: e.message }};
                            }}
                        }})()
                    """)

                broker_event(
                    f"Dashboard module response: {str(dashboard_response)[:500]}...",
                    logger=logger,
                )

                if dashboard_response and "error" not in dashboard_response:
                    investment_overview = None

                    if isinstance(dashboard_response, dict):
                        cache_entries = dashboard_response.get("cache", [])
                        for entry in cache_entries:
                            if entry.get("url") == INVESTMENT_OVERVIEW_CACHE_URL:
                                investment_overview = entry.get("response")
                                break

                        if not investment_overview and (
                            "investmentAccountOverviews" in dashboard_response
                            or "investmentAccountDetails" in dashboard_response
                        ):
                            investment_overview = dashboard_response
                    elif isinstance(dashboard_response, list):
                        investment_overview = {
                            "investmentAccountDetails": dashboard_response
                        }

                    if investment_overview:
                        invest_overviews = investment_overview.get(
                            "investmentAccountOverviews", []
                        )
                        overview = (
                            invest_overviews[0]
                            if invest_overviews
                            else investment_overview
                        )
                        account_details = overview.get(
                            "investmentAccountDetails",
                            overview.get("investmentAccountDetail", []),
                        )

                        if isinstance(account_details, list):
                            account_metadata = []
                            for item in account_details:
                                if isinstance(item, list):
                                    item = item[0] if item else {}
                                account_id = item.get("accountId") or item.get(
                                    "accountConnector"
                                )
                                if not account_id:
                                    continue
                                account_ids.append(account_id)
                                account_metadata.append(
                                    {
                                        "id": account_id,
                                        "name": item.get("nickname")
                                        or item.get("mask")
                                        or account_id,
                                        "mask": item.get("mask") or "",
                                    }
                                )

                            if account_metadata:
                                self._accounts = account_metadata
                    else:
                        try:
                            response_text = json.dumps(dashboard_response)
                            regexes = [
                                r'"accountId"\s*:\s*"([^"]+)"',
                                r'"accountConnector"\s*:\s*"([^"]+)"',
                                r'"accountNumber"\s*:\s*"([^"]+)"',
                            ]
                            for regex in regexes:
                                for match in re.findall(regex, response_text):
                                    if match not in account_ids:
                                        account_ids.append(match)
                        except Exception as exc:
                            logger.debug(
                                "Ignored transient error while parsing Chase account IDs from dashboard response",
                                exc_info=exc,
                            )

                if not self._accounts:
                    print("Fetching investment account details...")
                    overview_response = await self._page.evaluate(f"""
                        (async () => {{
                            try {{
                                const response = await fetch('{INVESTMENT_OVERVIEW_URL}', {{
                                    method: 'GET',
                                    credentials: 'include',
                                    headers: {{
                                        'Accept': 'application/json, text/plain, */*',
                                        'Content-Type': 'application/json',
                                        'x-jpmc-csrf-token': 'NONE',
                                        'x-requested-with': 'XMLHttpRequest',
                                        'x-jpmc-channel': 'id=C30',
                                        'origin': 'https://secure.chase.com',
                                        'referer': 'https://secure.chase.com/web/auth/dashboard'
                                    }}
                                }});
                                const text = await response.text();
                                if (response.ok) {{
                                    try {{
                                        return JSON.parse(text);
                                    }} catch (e) {{
                                        return {{ error: 'parse', text: text.substring(0, 1000) }};
                                    }}
                                }} else {{
                                    return {{ error: response.status, text: text.substring(0, 500) }};
                                }}
                            }} catch (e) {{
                                return {{ error: e.message }};
                            }}
                        }})()
                    """)

                    print(
                        f"Investment overview response: {str(overview_response)[:500]}..."
                    )

                    if overview_response and "error" not in overview_response:
                        overview = overview_response
                        invest_overviews = overview_response.get(
                            "investmentAccountOverviews", []
                        )
                        if invest_overviews:
                            overview = invest_overviews[0]

                        account_details = overview.get(
                            "investmentAccountDetails",
                            overview.get("investmentAccountDetail", []),
                        )
                        if isinstance(account_details, list):
                            account_metadata = []
                            for item in account_details:
                                if isinstance(item, list):
                                    item = item[0] if item else {}
                                account_id = item.get("accountId") or item.get(
                                    "accountConnector"
                                )
                                if not account_id:
                                    continue
                                if account_id not in account_ids:
                                    account_ids.append(account_id)
                                account_metadata.append(
                                    {
                                        "id": account_id,
                                        "name": item.get("nickname")
                                        or item.get("mask")
                                        or account_id,
                                        "mask": item.get("mask") or "",
                                    }
                                )
                            if account_metadata:
                                self._accounts = account_metadata

        except Exception as e:
            broker_event(
                f"Error getting account IDs: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()

        return account_ids

    def _parse_holdings_from_text(self, page_text):
        """Parse holdings from page text when API fails."""
        holdings = []

        try:
            # Debug: print the text
            print(f"=== Page text for parsing ===\n{page_text}\n=== End ===")

            # Simple approach: look for pattern like:
            # SYMBOL (1-5 uppercase letters on its own line)
            # followed eventually by numbers for price, value, quantity, cost

            lines = page_text.split("\n")
            i = 0
            while i < len(lines):
                line = lines[i].strip()

                # Check if this looks like a stock symbol
                if re.match(r"^[A-Z]{1,5}$", line):
                    symbol = line
                    # Skip common non-symbol words
                    if symbol in [
                        "ETF",
                        "USD",
                        "EST",
                        "AM",
                        "PM",
                        "ALL",
                        "DAY",
                        "NOT",
                        "AS",
                        "OF",
                        "NEW",
                    ]:
                        i += 1
                        continue

                    # Collect all numbers in the next 40 lines
                    numbers = []
                    for j in range(i + 1, min(i + 40, len(lines))):
                        next_line = lines[j].strip()

                        # Stop at next symbol or Trade button
                        if next_line == "Trade" or (
                            re.match(r"^[A-Z]{1,5}$", next_line)
                            and next_line not in ["ETF", "USD"]
                        ):
                            break

                        # Skip gain/loss lines
                        if "Gain of" in next_line or "Loss of" in next_line:
                            continue

                        # Split line on whitespace to find multiple numbers per line
                        # e.g., "20.46%  9.7" contains both a percentage and quantity
                        tokens = next_line.split()
                        for token in tokens:
                            # Skip tokens starting with +/- (gain/loss values)
                            if token.startswith("+") or token.startswith("-"):
                                continue
                            # Skip percentage values
                            if token.endswith("%"):
                                continue
                            # Try to parse as number
                            clean = token.replace(",", "").replace("$", "")
                            try:
                                num = float(clean)
                                numbers.append(num)
                            except ValueError:
                                pass

                    # We expect at least: price, value, quantity, cost
                    # From the page: 342.48, 3322.06, 9.7, 2757.83
                    if len(numbers) >= 4:
                        # Heuristic: price is first, then value (larger), then qty (smaller), then cost
                        price = numbers[0]
                        # Find quantity by looking for a number where price * qty ≈ some other number
                        for qi, qty in enumerate(numbers[1:], 1):
                            expected_value = price * qty
                            for vi, val in enumerate(numbers[1:], 1):
                                if (
                                    vi != qi
                                    and abs(val - expected_value) / max(val, 1) < 0.05
                                ):
                                    # Found it
                                    cost = (
                                        numbers[qi + 1]
                                        if qi + 1 < len(numbers)
                                        else None
                                    )
                                    holdings.append(
                                        {
                                            "symbol": symbol,
                                            "quantity": qty,
                                            "price": round(price, 2),
                                            "cost_basis": round(cost, 2)
                                            if cost
                                            else None,
                                            "current_value": round(val, 2),
                                        }
                                    )
                                    print(
                                        f"Parsed: {symbol} - {qty} shares @ ${price:.2f} = ${val:.2f}"
                                    )
                                    break
                            else:
                                continue
                            break
                i += 1

        except Exception as e:
            print(f"Error parsing holdings from text: {e}")
            traceback.print_exc()

        return holdings

    async def _fetch_holdings_json(self, account_id):
        """Fetch holdings for a specific account using fetch API from browser context."""
        holdings = []

        try:
            # Navigate to the positions page for this account to establish context
            if account_id:
                positions_url = f"https://secure.chase.com/web/auth/dashboard#/dashboard/oi-portfolio/positions/render;ai={account_id}"
            else:
                positions_url = "https://secure.chase.com/web/auth/dashboard#/dashboard/oi-portfolio/positions"

            print(f"Navigating to positions page...")
            await self._page.get(positions_url)
            await asyncio.sleep(2)

            for attempt in range(10):
                await asyncio.sleep(1)
                try:
                    page_text = await self._page.evaluate(
                        "document.body?.innerText || ''"
                    )
                    if len(page_text.strip()) > 50:
                        break
                    if attempt % 5 == 0:
                        print(f"Waiting for positions content... ({attempt + 1}s)")
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while waiting for Chase positions content",
                        exc_info=exc,
                    )

            api_response = None

            # Prefer capturing the network response (chaseinvest-api pattern)
            try:
                async with self._page.expect_response(
                    HOLDINGS_JSON_URL
                ) as response_info:
                    await self._page.reload()
                    await asyncio.wait_for(response_info.value, timeout=10)
                    body_str, _ = await response_info.response_body
                    api_response = json.loads(body_str)
                    print("Got holdings JSON response via network")
            except Exception as e:
                print(f"Network capture failed: {e}")

            if api_response is None:
                # Use fetch from within the page context (uses browser's auth cookies)
                print("Fetching holdings via API...")
                api_response = await self._page.evaluate(f"""
                    (async () => {{
                        try {{
                            const response = await fetch('{HOLDINGS_JSON_URL}', {{
                                method: 'GET',
                                credentials: 'include',
                                headers: {{
                                    'Accept': 'application/json, text/plain, */*',
                                    'Content-Type': 'application/json',
                                    'x-jpmc-csrf-token': 'NONE',
                                    'x-requested-with': 'XMLHttpRequest'
                                }}
                            }});
                            const text = await response.text();
                            console.log('API Response status:', response.status);
                            console.log('API Response text:', text.substring(0, 500));
                            if (response.ok) {{
                                try {{
                                    return JSON.parse(text);
                                }} catch (e) {{
                                    return {{ error: 'JSON parse error', text: text.substring(0, 500) }};
                                }}
                            }} else {{
                                return {{ error: response.status, statusText: response.statusText, text: text.substring(0, 500) }};
                            }}
                        }} catch (e) {{
                            return {{ error: e.message }};
                        }}
                    }})()
                """)

            print(f"API response: {str(api_response)[:500]}...")

            if api_response and "error" not in api_response:
                print("Got holdings JSON response")
                positions = api_response.get("positions", [])
                print(f"Found {len(positions)} positions in response")

                for pos in positions:
                    try:
                        symbol = pos.get("symbol", pos.get("tickerSymbol", ""))
                        if not symbol:
                            continue

                        quantity = float(
                            pos.get("quantity", pos.get("shareQuantity", 0))
                        )
                        if quantity <= 0:
                            continue

                        market_value = float(
                            pos.get("marketValue", pos.get("currentValue", 0))
                        )
                        price = market_value / quantity if quantity > 0 else 0
                        cost_basis = pos.get("costBasis", pos.get("totalCostBasis"))

                        holdings.append(
                            {
                                "symbol": symbol,
                                "quantity": quantity,
                                "price": round(price, 2),
                                "cost_basis": float(cost_basis) if cost_basis else None,
                                "current_value": round(market_value, 2),
                            }
                        )
                    except Exception as e:
                        print(f"Error parsing position: {e}")
                        continue

            else:
                error_msg = (
                    api_response.get("error", "Unknown error")
                    if api_response
                    else "No response"
                )
                print(f"API request failed: {error_msg}, trying page text parser...")

                # Fallback: parse holdings from page text
                page_text = await self._page.evaluate("document.body?.innerText || ''")
                print(f"Got page text ({len(page_text)} chars), parsing...")
                holdings = self._parse_holdings_from_text(page_text)
                print(f"Parser returned {len(holdings)} holdings")

        except Exception as e:
            broker_event(
                f"Error fetching holdings for account {account_id}: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()

        return holdings

    async def _discover_account_options(self):
        """Discover individual account options from the positions page dropdown."""
        accounts = []

        try:
            # Navigate to Positions page
            await self._page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/oi-portfolio/positions"
            )
            await asyncio.sleep(4)

            # Debug: find all clickable elements near "Investments by"
            dropdown_info = await self._page.evaluate("""
                (() => {
                    const results = {buttons: [], selects: [], dropdowns: [], links: []};

                    // Find all buttons
                    document.querySelectorAll('button').forEach(el => {
                        results.buttons.push({
                            text: el.textContent?.trim().substring(0, 50),
                            class: el.className,
                            id: el.id
                        });
                    });

                    // Find select elements
                    document.querySelectorAll('select').forEach(el => {
                        const opts = Array.from(el.options).map(o => o.text.trim());
                        results.selects.push({
                            id: el.id,
                            class: el.className,
                            options: opts
                        });
                    });

                    // Find dropdown-like elements
                    document.querySelectorAll('[role="listbox"], [role="menu"], [aria-expanded], [aria-haspopup]').forEach(el => {
                        results.dropdowns.push({
                            tag: el.tagName,
                            text: el.textContent?.trim().substring(0, 100),
                            role: el.getAttribute('role'),
                            expanded: el.getAttribute('aria-expanded')
                        });
                    });

                    // Find links with account-like URLs
                    document.querySelectorAll('a[href*="ai="], a[href*="account"]').forEach(el => {
                        results.links.push({
                            text: el.textContent?.trim().substring(0, 50),
                            href: el.href
                        });
                    });

                    return results;
                })()
            """)

            print(f"DEBUG - Buttons: {len(dropdown_info.get('buttons', []))}")
            for btn in dropdown_info.get("buttons", [])[:10]:
                print(f"  btn: {btn}")
            print(f"DEBUG - Selects: {len(dropdown_info.get('selects', []))}")
            for sel in dropdown_info.get("selects", []):
                print(f"  select: {sel}")
            print(f"DEBUG - Dropdowns: {len(dropdown_info.get('dropdowns', []))}")
            for dd in dropdown_info.get("dropdowns", [])[:10]:
                print(f"  dropdown: {dd}")
            print(f"DEBUG - Account links: {len(dropdown_info.get('links', []))}")
            for link in dropdown_info.get("links", []):
                print(f"  link: {link}")

            # Look for and click the account dropdown near "Investments by" label
            clicked = await self._page.evaluate("""
                (() => {
                    // Strategy 1: Find "Investments by" and click dropdown in same container
                    const allElements = document.querySelectorAll('*');
                    for (const el of allElements) {
                        if (el.textContent?.trim() === 'Investments by') {
                            // Look in parent container for dropdown
                            let parent = el.parentElement;
                            for (let i = 0; i < 5 && parent; i++) {
                                const dropdown = parent.querySelector('button:not([id*="brand"]), [role="combobox"], [aria-haspopup]');
                                if (dropdown && dropdown.textContent?.includes('Eligible')) {
                                    dropdown.click();
                                    return {clicked: true, method: 'Investments by sibling', tag: dropdown.tagName};
                                }
                                parent = parent.parentElement;
                            }
                        }
                    }

                    // Strategy 2: Find "All Eligible Accounts" text and click its container
                    for (const el of allElements) {
                        const text = el.textContent?.trim();
                        if (text === 'All Eligible Accounts') {
                            const clickTarget = el.closest('button, [role="combobox"]') || el;
                            clickTarget.click();
                            return {clicked: true, method: 'All Eligible text', tag: clickTarget.tagName};
                        }
                    }

                    // Strategy 3: Any button/dropdown containing "Eligible" or "Account"
                    const clickables = document.querySelectorAll('button, [role="combobox"], [aria-haspopup="true"]');
                    for (const el of clickables) {
                        const text = el.textContent?.trim() || '';
                        if ((text.includes('Eligible') || text.includes('Account')) && !el.id?.includes('brand')) {
                            el.click();
                            return {clicked: true, method: 'clickable with Eligible/Account', tag: el.tagName, text: text.substring(0, 40)};
                        }
                    }

                    return {clicked: false};
                })()
            """)
            print(f"DEBUG - Clicked account dropdown: {clicked}")
            await asyncio.sleep(2)  # Wait for dropdown animation

            # Now extract the account options from the opened dropdown
            accounts_dropdown = await self._page.evaluate("""
                (() => {
                    const results = [];
                    // Look for dropdown items - check role="option", listbox items, or menu items
                    const selectors = [
                        '[role="option"]',
                        '[role="menuitem"]',
                        '[role="listbox"] > *',
                        'ul[class*="dropdown"] li',
                        'div[class*="dropdown"] div[class*="item"]',
                        'li'
                    ];

                    for (const selector of selectors) {
                        document.querySelectorAll(selector).forEach(el => {
                            const text = el.textContent?.trim();
                            // Match patterns like "Self-Directed (...8539)" or account-like text
                            if (text && (text.match(/\\(\\.{3}\\d{4}\\)/) || text.includes('Self-Directed'))) {
                                results.push({
                                    text: text.substring(0, 60),
                                    tag: el.tagName,
                                    role: el.getAttribute('role')
                                });
                            }
                        });
                    }

                    // Also check for any visible text containing account patterns
                    if (results.length === 0) {
                        document.querySelectorAll('span, div, li, a').forEach(el => {
                            const text = el.textContent?.trim();
                            if (text && text.match(/Self-Directed.*\\d{4}/) && text.length < 50) {
                                results.push({
                                    text: text,
                                    tag: el.tagName,
                                    role: el.getAttribute('role')
                                });
                            }
                        });
                    }

                    return results;
                })()
            """)
            print(f"DEBUG - Account options found: {accounts_dropdown}")

            # Close the dropdown
            await self._page.evaluate("document.body.click()")
            await asyncio.sleep(0.5)

            # Look for account dropdown/selector and get options via JavaScript
            account_options = await self._page.evaluate("""
                (() => {
                    const results = [];

                    // Try to find dropdown options or account links
                    // Chase uses various selectors for account filtering

                    // Method 1: Look for select/dropdown options
                    const selects = document.querySelectorAll('select');
                    selects.forEach(sel => {
                        Array.from(sel.options).forEach(opt => {
                            if (opt.value && opt.text) {
                                results.push({type: 'select', value: opt.value, name: opt.text.trim()});
                            }
                        });
                    });

                    // Method 2: Look for account filter links/buttons with data attributes
                    document.querySelectorAll('[data-account-id], [data-accountid], a[href*="ai="]').forEach(el => {
                        const accountId = el.dataset.accountId || el.dataset.accountid ||
                            (el.href && el.href.match(/ai=([^&;#]+)/)?.[1]);
                        const name = el.textContent?.trim() || accountId;
                        if (accountId) {
                            results.push({type: 'link', value: accountId, name: name});
                        }
                    });

                    // Method 3: Look for account dropdown menu items
                    document.querySelectorAll('[role="menuitem"], [role="option"], .dropdown-item').forEach(el => {
                        const text = el.textContent?.trim();
                        const href = el.getAttribute('href') || '';
                        const aiMatch = href.match(/ai=([^&;#]+)/);
                        if (aiMatch) {
                            results.push({type: 'menu', value: aiMatch[1], name: text});
                        } else if (text && text.match(/\\d{4}/)) {
                            // Has 4-digit account number in text
                            results.push({type: 'text', value: text, name: text});
                        }
                    });

                    return results;
                })()
            """)

            print(f"Found {len(account_options)} account option(s) from page")
            for opt in account_options:
                print(f"  - {opt.get('name')} ({opt.get('value')}) [{opt.get('type')}]")

            # Deduplicate by value
            seen = set()
            for opt in account_options:
                if opt.get("value") and opt["value"] not in seen:
                    seen.add(opt["value"])
                    accounts.append(
                        {"id": opt["value"], "name": opt.get("name", opt["value"])}
                    )

            # Fallback: extract account IDs from page HTML if dropdown parsing fails
            if not accounts:
                html = await self._page.get_content()
                html_ids = set()
                html_ids.update(re.findall(r"ai=([A-Za-z0-9-]+)", html))
                html_ids.update(re.findall(r'data-account-id="([^"]+)"', html))
                html_ids.update(re.findall(r'data-account-number="([^"]+)"', html))
                html_ids.update(re.findall(r'"accountId"\s*:\s*"([^"]+)"', html))
                html_ids.update(re.findall(r'"accountConnector"\s*:\s*"([^"]+)"', html))
                for account_id in sorted(html_ids):
                    accounts.append({"id": account_id, "name": account_id})

        except Exception as e:
            print(f"Error discovering account options: {e}")
            traceback.print_exc()

        return accounts

    async def get_holdings(self, ticker=None):
        """Get holdings from all Chase accounts.

        Args:
            ticker: Optional ticker symbol to filter holdings

        Returns:
            Dictionary of account holdings or None if error
        """
        await self._ensure_authenticated()

        all_holdings = {}

        try:
            # First try to discover individual accounts from the positions page
            individual_accounts = await self._discover_account_options()

            # Also try API-based account discovery
            account_ids = await self._get_account_ids()

            account_name_map = {}

            if self._accounts:
                for acct in self._accounts:
                    account_id = acct.get("id")
                    if account_id and account_id not in account_name_map:
                        account_name_map[account_id] = acct.get("name") or account_id

            # Merge account sources
            if individual_accounts:
                # Use discovered accounts (they have proper names)
                for acct in individual_accounts:
                    if acct["id"] not in account_ids:
                        account_ids.append(acct["id"])
                    if acct.get("id"):
                        account_name_map[acct["id"]] = acct.get("name") or acct["id"]

            # Deduplicate while preserving order
            seen_ids = set()
            account_ids = [
                aid
                for aid in account_ids
                if aid and not (aid in seen_ids or seen_ids.add(aid))
            ]

            if not account_ids:
                # Fallback: navigate through UI and use aggregate view
                print("No individual accounts found, using aggregate view...")

                # Click on Investments, then Positions
                await self._page.evaluate("""
                    (() => {
                        // Click Investments nav
                        const inv = Array.from(document.querySelectorAll('a, button'))
                            .find(el => el.innerText?.trim() === 'Investments');
                        if (inv) inv.click();
                    })()
                """)
                await asyncio.sleep(2)

                await self._page.evaluate("""
                    (() => {
                        // Click Positions sub-nav
                        const pos = Array.from(document.querySelectorAll('a, button'))
                            .find(el => el.innerText?.trim() === 'Positions');
                        if (pos) pos.click();
                    })()
                """)
                await asyncio.sleep(3)

                # Now check the URL for account ID
                current_url = await self._page.evaluate("window.location.href")
                print(f"Current URL after navigation: {current_url}")

                # Try to extract account ID from URL
                ai_match = re.search(r"ai=([^&;#]+)", current_url)
                if ai_match:
                    account_ids = [ai_match.group(1)]
                    print(f"Found account ID in URL: {account_ids[0]}")
                else:
                    # Still try with empty account ID
                    account_ids = [""]

            print(f"Fetching holdings for {len(account_ids)} account(s): {account_ids}")

            for account_id in account_ids:
                holdings = await self._fetch_holdings_json(account_id)

                # Filter by ticker if specified
                if ticker:
                    holdings = [
                        h for h in holdings if h["symbol"].upper() == ticker.upper()
                    ]

                if holdings:
                    account_key = (
                        account_name_map.get(account_id) or account_id or "default"
                    )
                    all_holdings[account_key] = holdings
                    print(f"Account {account_key}: {len(holdings)} position(s)")

        except Exception as e:
            print(f"Error getting holdings: {e}")
            traceback.print_exc()

        return all_holdings if all_holdings else None

    async def trade(self, side, qty, ticker, price):
        """Execute a trade on Chase.

        Args:
            side: "buy" or "sell"
            qty: Number of shares
            ticker: Stock symbol
            price: Optional limit price (None for market order)

        Returns:
            True if trade successful, False otherwise
        """
        await self._ensure_authenticated()

        # Discover accounts
        accounts = await self._discover_accounts()
        success_count = 0

        for account in accounts:
            account_name = account["name"]
            print(f"\nTrading on: {account_name}")

            try:
                # Navigate to trade page
                await self._page.get(
                    "https://secure.chase.com/web/auth/dashboard#/dashboard/investments/youInvest/trade"
                )
                await asyncio.sleep(3)

                # Enter symbol
                print(f"Entering symbol: {ticker}")
                symbol_input = await self._page.select(
                    "input[id=symbol-search], input[name=symbol]", timeout=10
                )
                await symbol_input.click()
                await symbol_input.clear_input()
                await symbol_input.send_keys(ticker)
                await asyncio.sleep(1)

                # Press enter to load quote
                await symbol_input.send_keys("\n")
                await asyncio.sleep(2)

                # Select action (Buy/Sell)
                action_text = "buy" if side == "buy" else "sell"
                print(f"Setting action to {action_text.upper()}")

                # Click buy or sell button
                action_selector = f"button[data-action={action_text}], button:contains('{action_text.capitalize()}')"
                try:
                    action_button = await self._page.select(action_selector, timeout=5)
                    await action_button.click()
                    await asyncio.sleep(1)
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    print(
                        f"Could not find {action_text} button with selector {action_selector}"
                    )
                    # Try JavaScript click
                    await self._page.evaluate(f"""
                        Array.from(document.querySelectorAll('button')).find(btn => 
                            btn.textContent.toLowerCase().includes('{action_text}')
                        )?.click()
                    """)
                    await asyncio.sleep(1)

                # Enter quantity
                print(f"Setting quantity to {qty}")
                qty_input = await self._page.select(
                    "input[id=quantity], input[name=quantity]", timeout=5
                )
                await qty_input.click()
                await qty_input.clear_input()
                await qty_input.send_keys(str(qty))
                await asyncio.sleep(1)

                # Set order type (Market or Limit)
                if price:
                    print(f"Setting limit price to ${price}")
                    # Select limit order
                    limit_button = await self._page.select(
                        "button[data-order-type=limit], input[value=LIMIT]", timeout=5
                    )
                    await limit_button.click()
                    await asyncio.sleep(1)

                    # Enter limit price
                    price_input = await self._page.select(
                        "input[id=limit-price], input[name=limitPrice]", timeout=5
                    )
                    await price_input.click()
                    await price_input.clear_input()
                    await price_input.send_keys(str(price))
                    await asyncio.sleep(1)
                else:
                    print("Using market order")
                    market_button = await self._page.select(
                        "button[data-order-type=market], input[value=MARKET]", timeout=5
                    )
                    await market_button.click()
                    await asyncio.sleep(1)

                # Check for dry run
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    print(
                        f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}"
                    )
                    success_count += 1
                    continue

                # Click preview order button
                print("Clicking preview/review order...")
                preview_selectors = [
                    "button[id=preview-order]",
                    "button[id=review-order]",
                    "button:contains('Preview')",
                    "button:contains('Review')",
                ]

                preview_button = None
                for selector in preview_selectors:
                    try:
                        preview_button = await self._page.select(selector, timeout=2)
                        if preview_button:
                            break
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        continue

                if not preview_button:
                    # Try finding by text
                    preview_button = await self._page.evaluate("""
                        Array.from(document.querySelectorAll('button')).find(btn => 
                            btn.textContent.toLowerCase().includes('preview') || 
                            btn.textContent.toLowerCase().includes('review')
                        )
                    """)

                if preview_button:
                    await preview_button.click()
                    await asyncio.sleep(3)
                else:
                    print("Could not find preview button")
                    continue

                # Check for errors on preview page
                page_text = await self._page.evaluate("document.body.textContent")
                if "error" in page_text.lower() and "warning" not in page_text.lower():
                    print(f"Error on preview page for {account_name}")
                    continue

                # Click submit/place order button
                print("Placing order...")
                submit_selectors = [
                    "button[id=submit-order]",
                    "button[id=place-order]",
                    "button:contains('Submit')",
                    "button:contains('Place Order')",
                ]

                submit_button = None
                for selector in submit_selectors:
                    try:
                        submit_button = await self._page.select(selector, timeout=2)
                        if submit_button:
                            break
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        continue

                if not submit_button:
                    # Try finding by text
                    submit_button = await self._page.evaluate("""
                        Array.from(document.querySelectorAll('button')).find(btn => 
                            btn.textContent.toLowerCase().includes('submit') || 
                            btn.textContent.toLowerCase().includes('place order')
                        )
                    """)

                if submit_button:
                    await submit_button.click()
                    await asyncio.sleep(3)
                else:
                    print("Could not find submit button")
                    continue

                # Check for success
                page_text = await self._page.evaluate("document.body.textContent")
                success_patterns = [
                    "order received",
                    "successfully",
                    "confirmation",
                    "order number",
                    "has been placed",
                    "submitted",
                ]

                is_success = any(
                    pattern in page_text.lower() for pattern in success_patterns
                )

                if is_success:
                    action_str = "Bought" if side == "buy" else "Sold"
                    broker_event(
                        f"✓ {action_str} {qty} shares of {ticker} on {account_name}",
                        logger=logger,
                    )
                    success_count += 1
                else:
                    broker_event(
                        f"Chase order may have failed on {account_name}",
                        level="warning",
                        logger=logger,
                    )

            except Exception as e:
                broker_event(
                    f"Error trading on {account_name}: {e}",
                    level="error",
                    logger=logger,
                    exc=e,
                )
                traceback.print_exc()
                continue

        return success_count > 0


async def chaseGetHoldings(ticker=None):
    """Get holdings from all Chase accounts."""
    from .base import rate_limiter

    await rate_limiter.wait_if_needed("Chase")

    from .session_manager import session_manager

    session = await session_manager.get_session("Chase")
    if not session:
        broker_event(
            "No Chase credentials supplied, skipping",
            level="warning",
            logger=logger,
        )
        return None

    headless = os.getenv("HEADLESS", "false").lower() == "true"
    try:
        async with ChaseClient(
            username=session["username"],
            password=session["password"],
            headless=headless,
        ) as client:
            return await client.get_holdings(ticker)
    except Exception as e:
        broker_event(
            f"Error getting Chase holdings: {e}",
            level="error",
            logger=logger,
            exc=e,
        )
        traceback.print_exc()
        return None


async def chaseTrade(side, qty, ticker, price):
    """Execute a trade on Chase.

    Returns:
        True: Trade executed successfully
        False: Trade failed
        None: No credentials (broker skipped)
    """
    from .base import rate_limiter

    await rate_limiter.wait_if_needed("Chase")

    from .session_manager import session_manager

    session = await session_manager.get_session("Chase")
    if not session:
        broker_event(
            "No Chase credentials supplied, skipping",
            level="warning",
            logger=logger,
        )
        return None

    headless = os.getenv("HEADLESS", "false").lower() == "true"
    try:
        async with ChaseClient(
            username=session["username"],
            password=session["password"],
            headless=headless,
        ) as client:
            return await client.trade(side, qty, ticker, price)
    except Exception as e:
        broker_event(
            f"Error during Chase trade: {e}",
            level="error",
            logger=logger,
            exc=e,
        )
        traceback.print_exc()
        return False


async def get_chase_session(session_manager):
    """Get or create Chase session."""
    if "chase" not in session_manager._initialized:
        CHASE_USER = os.getenv("CHASE_USER")
        CHASE_PASS = os.getenv("CHASE_PASS")

        if not (CHASE_USER and CHASE_PASS):
            session_manager.sessions["chase"] = None
            session_manager._initialized.add("chase")
            return None

        try:
            # Store credentials for later use by trade/holdings functions
            chase_session = {
                "username": CHASE_USER,
                "password": CHASE_PASS,
            }

            session_manager.sessions["chase"] = chase_session
            broker_event("✓ Chase credentials loaded", logger=logger)
        except Exception as e:
            broker_event(
                f"✗ Failed to initialize Chase session: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            session_manager.sessions["chase"] = None

        session_manager._initialized.add("chase")

    return session_manager.sessions.get("chase")
