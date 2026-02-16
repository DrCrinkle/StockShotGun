"""Wells Fargo Advisors broker integration using zendriver."""

import os
import re
import asyncio
import logging
from bs4 import BeautifulSoup
from brokers.browser_utils import create_browser, stop_browser, get_page_url, get_page_title, navigate_and_wait, wait_for_ready_state, poll_for_condition
from brokers.base import rate_limiter, broker_event

logger = logging.getLogger(__name__)


class WellsFargoClient:
    """Wells Fargo Advisors client with browser automation."""

    COOKIES_PATH = "./tokens/wellsfargo_cookies.pkl"

    def __init__(
        self,
        username: str,
        password: str,
        phone_suffix: str = "",
        headless: bool = True,
    ):
        """Initialize Wells Fargo client with credentials.

        Args:
            username: Wells Fargo username
            password: Wells Fargo password
            phone_suffix: Optional phone number suffix for 2FA
            headless: Run browser in headless mode
        """
        self._username = username
        self._password = password
        self._phone_suffix = phone_suffix
        self._headless = headless

        # State management
        self._browser = None
        self._page = None
        self._is_authenticated = False

    async def __aenter__(self):
        """Async context manager entry. Browser created lazily on first operation."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit. Cleanup browser if exists."""
        if self._browser:
            await stop_browser(self._browser, log=logger)
            self._browser = None
            self._page = None
            self._is_authenticated = False

    async def _ensure_authenticated(self):
        """Ensure browser is authenticated. Lazy authentication - only creates browser when needed."""
        if not self._is_authenticated or not self._browser:
            await self._authenticate()

    async def _is_logged_in(self, page):
        """Check if the current page indicates an authenticated session."""
        url = (await get_page_url(page)).lower()
        title = (await get_page_title(page)).lower()
        return (
            any(m in url for m in ("wellstrade", "brokoverview"))
            and any(m in title for m in ("brokerage overview", "wellstrade"))
        )

    async def _try_restore_cookies(self):
        """Try to restore a previous session from saved cookies.

        Returns True if session was restored, False otherwise.
        """
        try:
            await self._browser.cookies.load(self.COOKIES_PATH)
            page = await self._browser.get(
                "https://wfawellstrade.wellsfargo.com/BW/brokoverview.do"
            )
            await wait_for_ready_state(page)

            if await self._is_logged_in(page):
                broker_event("✓ Wells Fargo session restored from cookies", logger=logger)
                self._page = page
                self._is_authenticated = True
                return True
        except (ValueError, FileNotFoundError, OSError):
            pass
        except Exception as e:
            logger.debug("Cookie restore failed: %s", e)
        return False

    async def _save_cookies(self):
        """Save current browser cookies for future sessions."""
        try:
            await self._browser.cookies.save(self.COOKIES_PATH)
            logger.debug("Saved Wells Fargo cookies")
        except Exception as e:
            logger.debug("Could not save cookies: %s", e)

    async def _authenticate(self):
        """Authenticate with Wells Fargo using browser automation."""
        try:
            # Start browser
            self._browser = await create_browser(headless=self._headless)

            # Try restoring previous session from cookies
            if await self._try_restore_cookies():
                return

            # Navigate to Wells Fargo Advisors login
            self._page = await self._browser.get("https://www.wellsfargoadvisors.com/")
            page = self._page
            await wait_for_ready_state(page)

            # Enter username
            username_input = await page.select("input[id=j_username]")
            await username_input.click()
            await username_input.clear_input()
            await username_input.send_keys(self._username)

            # Enter password
            password_input = await page.select("input[id=j_password]")
            await password_input.send_keys(self._password)

            # Click sign on button
            sign_on_button = await page.select(".button.button--login.button--signOn")
            await sign_on_button.click()
            broker_event("Waiting for login to process...", logger=logger)

            # Wait and check multiple times for navigation
            login_verified = False
            needs_additional_verification = False
            current_url = page.url
            page_title = ""

            success_url_markers = ("wellstrade", "brokoverview")
            success_title_markers = ("brokerage overview", "wellstrade")

            for attempt in range(
                12
            ):  # Give the login a little longer before assuming puzzle
                await asyncio.sleep(1)

                current_url = await get_page_url(page)
                page_title = await get_page_title(page)

                url_lower = (current_url or "").lower()
                title_lower = page_title.lower()

                if any(marker in url_lower for marker in success_url_markers) or any(
                    marker in title_lower for marker in success_title_markers
                ):
                    if not login_verified:
                        broker_event("Successfully logged in!", logger=logger)
                    login_verified = True
                    break

                if "interdiction" in url_lower:
                    needs_additional_verification = True
                    break

            if needs_additional_verification and not login_verified:
                broker_event(
                    "Login requires additional Wells Fargo verification (e.g. 2FA).",
                    level="warning",
                    logger=logger,
                )

            # Check if we got an error (anti-bot check or CAPTCHA)
            if not login_verified and not needs_additional_verification:
                broker_event(
                    "Login not completing - please check the browser window for CAPTCHA/puzzle and log in manually",
                    level="warning",
                    logger=logger,
                )

                async def _check_login():
                    url = (await get_page_url(page)).lower()
                    title = (await get_page_title(page)).lower()
                    return ("wellstrade" in url or "brokoverview" in url) and (
                        "brokerage overview" in title or "wellstrade" in title
                    )

                if await poll_for_condition(_check_login, timeout=120, interval=2):
                    broker_event("✓ Login successful!", logger=logger)
                    login_verified = True

            # Handle 2FA if needed (check for INTERDICTION in URL as per reference)
            current_url = await get_page_url(page)

            if "dest=INTERDICTION" in current_url:
                broker_event(
                    "Wells Fargo 2FA required - please complete verification in the browser window",
                    level="warning",
                    logger=logger,
                )

                async def _check_2fa():
                    url = (await get_page_url(page)).lower()
                    return "interdiction" not in url

                if await poll_for_condition(_check_2fa, timeout=120, interval=2):
                    broker_event("✓ 2FA completed successfully", logger=logger)

            current_url = await get_page_url(page)
            page_title = await get_page_title(page)

            # Check if we're already on the overview/accounts page
            already_on_overview = any(
                keyword in current_url.lower()
                for keyword in ["brokoverview", "wellstrade"]
            ) or any(
                keyword in page_title.lower()
                for keyword in ["brokerage overview", "wellstrade"]
            )

            if "login" in current_url.lower() and not already_on_overview:
                raise Exception(f"Wells Fargo login failed - still on login page: {current_url}")

            # Navigate to accounts overview page to enable account discovery
            # This ensures we have the accounts table available
            broker_event(
                "Login successful! Loading account information...", logger=logger
            )

            if not already_on_overview:
                try:
                    await navigate_and_wait(
                        page, "https://wfawellstrade.wellsfargo.com/BW/brokoverview.do"
                    )
                except Exception as e:
                    broker_event(
                        f"Warning: Could not navigate to overview page: {e}",
                        level="warning",
                        logger=logger,
                        exc=e,
                    )
            broker_event("✓ Wells Fargo authenticated successfully", logger=logger)
            await self._save_cookies()
            self._page = page
            self._is_authenticated = True

        except Exception as e:
            broker_event(
                f"Error during Wells Fargo authentication: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            if self._browser:
                await self._browser.stop()
                self._browser = None
            self._is_authenticated = False
            raise

    async def _extract_x_param(self):
        """Extract the dynamic _x parameter from current URL using regex."""
        try:
            current_url = await get_page_url(self._page)

            match = re.search(r"_x=([^&]+)", current_url)
            if match:
                return f"_x={match.group(1)}"
            return ""
        except Exception as e:
            broker_event(
                f"Error extracting x_param: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            return ""

    async def _discover_accounts(self):
        """
        Discover all Wells Fargo accounts by parsing the accounts page.
        Returns list of dicts with account info: {index, name, number, balance, x_param}
        """
        try:
            # Ensure we're on the accounts overview page; if not, navigate there and wait
            current_url = await get_page_url(self._page)

            if "brokoverview" not in (current_url or "").lower():
                try:
                    await navigate_and_wait(
                        self._page, "https://wfawellstrade.wellsfargo.com/BW/brokoverview.t.do"
                    )
                except Exception as nav_err:
                    logger.warning("Unable to navigate to accounts overview: %s", nav_err)
            else:
                # Give the overview table a moment to populate after login
                await asyncio.sleep(2)

            # Poll for the accounts table to populate before parsing to capture all accounts
            soup = BeautifulSoup("", "html.parser")
            account_rows = []
            for attempt in range(6):
                try:
                    row_count = await self._page.evaluate(
                        "document.querySelectorAll('tr[data-p_account]').length"
                    )
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    row_count = 0

                if row_count > 0:
                    html = await self._page.get_content()
                    soup = BeautifulSoup(html, "html.parser")
                    account_rows = soup.select("tr[data-p_account]")
                    if account_rows:
                        break

                await asyncio.sleep(2)

            # Extract x parameter from URL
            x_param = await self._extract_x_param()
            broker_event(
                f"Extracted x_param: {x_param[:50] if x_param else 'None'}...",
                logger=logger,
            )

            # If still no rows found, fall back to parsing whatever content is available
            if not account_rows:
                html = await self._page.get_content()
                soup = BeautifulSoup(html, "html.parser")
                account_rows = soup.select("tr[data-p_account]")

            accounts = []
            account_index = 0  # Separate counter for non-"-1" accounts
            for idx, row in enumerate(account_rows):
                # Skip "All Accounts" row (data-p_account="-1")
                account_attr = row.get("data-p_account")
                if account_attr == "-1":
                    continue

                try:
                    # Get account name from rowheader
                    name_elem = row.select_one('[role="rowheader"] .ellipsis')
                    account_name = (
                        name_elem.get_text(strip=True)
                        if name_elem
                        else f"Account {account_index}"
                    )

                    # Get account number
                    number_elem = row.select_one("div:not(.ellipsis-container)")
                    account_number = ""
                    if number_elem:
                        account_number = number_elem.get_text(strip=True).replace(
                            "*", ""
                        )

                    # Get balance from last td with data-sort-value
                    balance_cells = row.select("td[data-sort-value]")
                    balance = 0.0
                    if balance_cells:
                        balance_text = balance_cells[-1].get_text(strip=True)
                        # Remove currency symbols and commas
                        balance_text = balance_text.replace("$", "").replace(",", "")
                        try:
                            balance = float(balance_text)
                        except (ValueError, TypeError):
                            pass

                    accounts.append(
                        {
                            "index": account_index,  # Use separate counter, not enumerate idx
                            "data_p_account": account_attr,  # Store the actual attribute value too
                            "name": account_name,
                            "number": account_number,
                            "balance": balance,
                            "x_param": x_param,
                        }
                    )

                    broker_event(
                        f"Found account #{account_index}: {account_name} ({account_number}) - ${balance:,.2f} [data-p_account={account_attr}]",
                        logger=logger,
                    )
                    account_index += 1

                except Exception as row_err:
                    broker_event(
                        f"Error parsing account row {idx}: {row_err}",
                        level="error",
                        logger=logger,
                        exc=row_err,
                    )
                    continue

            if not accounts:
                broker_event(
                    "Warning: No accounts found on page",
                    level="warning",
                    logger=logger,
                )
                # Fallback: return single account with index 0
                accounts = [
                    {
                        "index": 0,
                        "name": "Default Account",
                        "number": "",
                        "balance": 0.0,
                        "x_param": x_param,
                    }
                ]

            return accounts

        except Exception as e:
            logger.error("Error discovering accounts: %s", e, exc_info=e)
            # Fallback to single account
            x_param = await self._extract_x_param() if self._page else ""
            return [
                {
                    "index": 0,
                    "name": "Default Account",
                    "number": "",
                    "balance": 0.0,
                    "x_param": x_param,
                }
            ]

    async def _parse_holdings_table(self, html):
        """Parse Wells Fargo holdings table HTML using exact selectors."""
        soup = BeautifulSoup(html, "html.parser")
        holdings = []

        # Find holdings rows using exact selector: tbody > tr.level1
        rows = soup.select("tbody > tr.level1")

        for row in rows:
            try:
                # Symbol: a.navlink.quickquote (remove ",popup" suffix as per reference)
                symbol_elem = row.select_one("a.navlink.quickquote")
                if not symbol_elem:
                    continue

                symbol_text = symbol_elem.get_text(strip=True)
                symbol = symbol_text.split(",")[0].strip()

                if not symbol or symbol.lower() == "popup":
                    # Skip empty helper rows
                    continue

                # Name: td[role="rowheader"] .data-content > div:last-child
                name_elem = row.select_one(
                    'td[role="rowheader"] .data-content > div:last-child'
                )
                name = name_elem.get_text(strip=True) if name_elem else "N/A"

                # Quantity and Price: td.datanumeric cells
                data_cells = row.select("td.datanumeric")
                if len(data_cells) < 3:
                    continue

                # Quantity is index [1], price is index [2]
                quantity_cell = data_cells[1]
                quantity_div = quantity_cell.select_one("div:first-child")
                quantity_text = (
                    quantity_div.get_text(strip=True) if quantity_div else "0"
                )
                quantity = float(quantity_text.replace(",", ""))

                price_cell = data_cells[2]
                price_div = price_cell.select_one("div:first-child")
                price_text = price_div.get_text(strip=True) if price_div else "0"
                price = float(price_text.replace("$", "").replace(",", ""))

                current_value = quantity * price
                cost_basis = None

                # Only add if quantity > 0
                if quantity > 0:
                    holdings.append(
                        {
                            "symbol": symbol,
                            "name": name,
                            "quantity": quantity,
                            "price": price,
                            "cost_basis": cost_basis,
                            "current_value": current_value,
                        }
                    )

            except (ValueError, IndexError, AttributeError) as e:
                logger.debug("Error parsing holdings row: %s", e)
                continue

        return holdings

    async def _goto_holdings(self, account_index, x_param):
        """Navigate to holdings page for the specified account."""
        holdings_url = f"https://wfawellstrade.wellsfargo.com/BW/holdings.do?account={account_index}"
        if x_param:
            holdings_url += f"&{x_param}"

        logger.debug("Navigating to holdings: %s", holdings_url)
        await navigate_and_wait(self._page, holdings_url)

    async def _goto_trade_form(self, account_param, ticker, side, x_param):
        """Navigate to trade form for the specified account and ticker."""
        action_value = "BUY" if side == "buy" else "SELL"
        trade_url = f"https://wfawellstrade.wellsfargo.com/BW/equity.do?account={account_param}&symbol={ticker}&selectedAction={action_value}"
        if x_param:
            trade_url += f"&{x_param}"

        logger.debug("Navigating to trade: %s", trade_url)
        await navigate_and_wait(self._page, trade_url)

    async def get_holdings(self, ticker=None):
        """Get holdings from all Wells Fargo accounts.

        Args:
            ticker: Optional ticker symbol to filter holdings

        Returns:
            Dictionary of account holdings or None if error
        """
        await self._ensure_authenticated()

        # Discover all accounts
        broker_event("Discovering Wells Fargo accounts...", logger=logger)
        accounts = await self._discover_accounts()
        broker_event(f"Found {len(accounts)} Wells Fargo account(s)", logger=logger)

        all_holdings = {}

        # Iterate through each account
        for account in accounts:
            account_name = account["name"]
            account_number = account["number"]
            account_index = account["index"]
            x_param = account["x_param"]

            broker_event(f"Fetching holdings for: {account_name}", logger=logger)

            try:
                # Navigate to holdings page for this account
                await self._goto_holdings(account_index, x_param)

                # Check if we got an error page
                page_content = await self._page.get_content()
                page_content_lower = page_content.lower()

                cloudflare_markers = (
                    "error occurred",
                    "attention required! | cloudflare",
                    "cf-error",
                    "cf-browser-verification",
                    "cf-chl",
                    "cloudflare ray id",
                )

                if any(marker in page_content_lower for marker in cloudflare_markers):
                    broker_event(f"Error page detected for {account_name}", level="warning", logger=logger)
                    continue

                # Reuse previously fetched HTML
                html = page_content

                # Parse holdings
                holdings = await self._parse_holdings_table(html)

                # Filter by ticker if specified
                if ticker:
                    holdings = [
                        h for h in holdings if h["symbol"].upper() == ticker.upper()
                    ]

                if holdings:
                    # Use account number or name as key
                    account_key = account_number if account_number else account_name
                    all_holdings[account_key] = holdings
                    broker_event(
                        f"Found {len(holdings)} position(s) in {account_name}",
                        logger=logger,
                    )
                else:
                    broker_event(
                        f"No holdings found in {account_name}",
                        level="warning",
                        logger=logger,
                    )

            except Exception as e:
                broker_event(
                    f"Error getting holdings for {account_name}: {e}",
                    level="error",
                    logger=logger,
                    exc=e,
                )

                continue

        return all_holdings if all_holdings else None

    async def trade(self, side, qty, ticker, price):
        """Execute a trade on Wells Fargo Advisors.

        Args:
            side: "buy" or "sell"
            qty: Number of shares
            ticker: Stock symbol
            price: Optional limit price (None for market order)

        Returns:
            Number of successful trades or None if error
        """
        # Save original price parameter (user-specified price, if any)
        user_specified_price = price

        await self._ensure_authenticated()

        accounts = await self._discover_accounts()
        broker_event(f"Found {len(accounts)} Wells Fargo account(s)", logger=logger)

        success_count = 0

        # Trade on all accounts
        for account in accounts:
            account_name = account["name"]
            # Use data_p_account if available, otherwise fall back to index
            account_param = account.get("data_p_account", account["index"])
            x_param = account["x_param"]

            # Reset price for this account (use per-account copy to avoid stale pricing)
            price_for_account = user_specified_price

            broker_event(f"Trading on: {account_name}", logger=logger)

            try:
                # Navigate to trade page for this account WITH symbol pre-filled
                await self._goto_trade_form(account_param, ticker, side, x_param)

                await self._page.wait_for("body", timeout=10)

                # Set buy/sell action
                action_text = "Buy" if side == "buy" else "Sell"
                try:
                    result = await self._page.evaluate(f"""
                        (function() {{
                            const buySellInput = document.getElementById('BuySell');
                            const buySellBtn = document.getElementById('BuySellBtn');
                            if (buySellInput && buySellBtn) {{
                                buySellInput.value = '{action_text}';
                                buySellBtn.textContent = '{action_text}';
                                buySellBtn.setAttribute('aria-label', '{action_text}');
                                return true;
                            }}
                            return false;
                        }})()
                    """)
                    if result:
                        logger.debug("Set action to %s", action_text)
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("Could not find BuySell elements")
                except Exception as e:
                    logger.warning("Could not set BuySell: %s", e)

                # Wait for quote to load
                await asyncio.sleep(2)
                try:
                    quote_loaded = await self._page.evaluate(
                        "document.getElementById('last')?.value"
                    )
                    if quote_loaded and quote_loaded != "None" and quote_loaded.strip():
                        logger.debug("Quote loaded: $%s", quote_loaded)
                    else:
                        logger.debug("Quote may not have loaded (value: %r)", quote_loaded)
                except Exception as e:
                    logger.debug("Error checking quote: %s", e)

                # Enter quantity
                try:
                    result = await self._page.evaluate(f"""
                        (function() {{
                            const qtyInput = document.getElementById('OrderQuantity');
                            if (qtyInput) {{
                                qtyInput.value = '{qty}';
                                qtyInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                qtyInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                return qtyInput.value;
                            }}
                            return null;
                        }})()
                    """)
                    if result:
                        logger.debug("Quantity set to %s", result)
                        await asyncio.sleep(0.5)
                    else:
                        logger.warning("Could not find OrderQuantity input field")
                        continue
                except Exception as e:
                    logger.warning("Error setting quantity: %s", e)
                    continue

                # Get last price from page for limit orders
                if not price_for_account:
                    try:
                        last_price_str = await self._page.evaluate(
                            "document.getElementById('last')?.value"
                        )
                        if last_price_str and last_price_str.strip():
                            price_for_account = float(last_price_str.strip())
                            logger.debug("Current stock price: $%s", price_for_account)
                        else:
                            logger.debug("Could not get price from page, defaulting to $1")
                            price_for_account = 1.00
                    except Exception as e:
                        logger.debug("Error getting price: %s", e)
                        price_for_account = 1.00

                # Determine order type
                use_limit_order = False
                limit_reason = ""

                if user_specified_price:
                    use_limit_order = True
                    limit_reason = "user specified price"
                elif price_for_account and price_for_account < 2.00:
                    use_limit_order = True
                    limit_reason = "low-priced stock (Wells Fargo requirement)"

                if use_limit_order:
                    logger.debug("Setting order type to LIMIT (%s)", limit_reason)
                    try:
                        result = await self._page.evaluate("""
                            (function() {
                                const priceQualInput = document.getElementById('PriceQualifier');
                                const orderTypeBtn = document.getElementById('OrderTypeBtn');
                                if (priceQualInput && orderTypeBtn) {
                                    priceQualInput.value = 'Limit';
                                    orderTypeBtn.textContent = 'Limit';
                                    return true;
                                }
                                return false;
                            })()
                        """)
                        if result:
                            logger.debug("Set order type to Limit")
                    except Exception as e:
                        logger.warning("Could not set order type: %s", e)

                    await asyncio.sleep(0.5)

                    # Calculate limit price
                    if user_specified_price:
                        adjusted_price = round(user_specified_price, 2)
                        logger.debug("Using user-specified price $%s", adjusted_price)
                    else:
                        if side == "buy":
                            adjusted_price = round(price_for_account + 0.01, 2)
                            logger.debug("Using last price $%s + $0.01 for buy", price_for_account)
                        else:
                            adjusted_price = round(price_for_account - 0.01, 2)
                            logger.debug("Using last price $%s - $0.01 for sell", price_for_account)

                    if adjusted_price <= 0:
                        adjusted_price = 0.01

                    try:
                        result = await self._page.evaluate(f"""
                            (function() {{
                                const priceInput = document.getElementById('Price');
                                if (priceInput) {{
                                    priceInput.value = '{adjusted_price}';
                                    priceInput.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    priceInput.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    return true;
                                }}
                                return false;
                            }})()
                        """)
                        if result:
                            logger.debug("Limit price set to $%s", adjusted_price)
                    except Exception as e:
                        logger.warning("Could not set limit price: %s", e)
                else:
                    logger.debug("Setting order type to MARKET")
                    try:
                        result = await self._page.evaluate("""
                            (function() {
                                const priceQualInput = document.getElementById('PriceQualifier');
                                const orderTypeBtn = document.getElementById('OrderTypeBtn');
                                if (priceQualInput && orderTypeBtn) {
                                    priceQualInput.value = 'Market';
                                    orderTypeBtn.textContent = 'Market';
                                    return true;
                                }
                                return false;
                            })()
                        """)
                        if result:
                            logger.debug("Set order type to Market")
                            await asyncio.sleep(0.5)
                    except Exception as e:
                        logger.warning("Could not set order type: %s", e)

                # Set time in force to Day
                logger.debug("Setting time in force to DAY")
                try:
                    result = await self._page.evaluate("""
                        (function() {
                            const tifInput = document.getElementById('TIF');
                            const tifBtn = document.getElementById('TIFBtn');
                            if (tifInput && tifBtn) {
                                tifInput.value = 'Day';
                                tifBtn.textContent = 'Day';
                                return true;
                            }
                            return false;
                        })()
                    """)
                    if result:
                        logger.debug("Set TIF to Day")
                        await asyncio.sleep(0.5)
                except Exception as e:
                    logger.warning("Could not set TIF: %s", e)

                await asyncio.sleep(1)

                # Click continue button
                logger.debug("Clicking continue button")
                continue_button = await self._page.select(
                    "button[id=actionbtnContinue]", timeout=5
                )
                if continue_button:
                    await continue_button.click()
                    await wait_for_ready_state(self._page)
                else:
                    logger.warning("Could not find continue button for %s", account_name)
                    continue

                # Check for errors on preview page
                try:
                    error_texts = await self._page.evaluate("document.body.textContent || ''")

                    has_errors = False
                    error_messages = []
                    if error_texts:
                        lines = error_texts.split("\\n")
                        for line in lines:
                            line = line.strip()
                            if line.startswith("Error:") and "Warning:" not in line:
                                has_errors = True
                                error_messages.append(line[:200])
                                if len(error_messages) >= 3:
                                    break

                    if has_errors:
                        broker_event(
                            f"Order errors for {account_name}: {'; '.join(error_messages)}",
                            level="error",
                            logger=logger,
                        )
                        continue
                    else:
                        logger.debug("Order preview looks good")
                except Exception as ex:
                    logger.debug("Error checking for validation errors: %s", ex)

                # Find confirm button
                confirm_button = None
                try:
                    confirm_button = await self._page.select(
                        ".btn-wfa-primary.btn-wfa-submit", timeout=5
                    )
                    if confirm_button:
                        logger.debug("Found submit button (.btn-wfa-primary.btn-wfa-submit)")
                except Exception as exc:
                    logger.debug("Primary submit selector not found, trying fallbacks", exc_info=exc)
                    for button_id in ["actionbtnContinue", "confirmBtn", "submitBtn"]:
                        try:
                            confirm_button = await self._page.select(
                                f"button[id={button_id}]", timeout=2
                            )
                            if confirm_button:
                                logger.debug("Found confirm button: %s", button_id)
                                break
                        except (
                            asyncio.TimeoutError,
                            AttributeError,
                            RuntimeError,
                            TypeError,
                        ):
                            continue

                if not confirm_button:
                    broker_event(
                        f"No confirmation button found for {account_name}",
                        level="error",
                        logger=logger,
                    )
                    continue

                # Check dry-run
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    broker_event(
                        f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}",
                        logger=logger,
                    )
                    success_count += 1
                    continue

                # Submit order
                logger.debug("Submitting order")
                await confirm_button.click()
                await wait_for_ready_state(self._page)

                # Check for success
                try:
                    page_text = await self._page.evaluate("document.body.textContent")

                    success_patterns = [
                        "order has been placed",
                        "successfully",
                        "confirmed",
                        "order number",
                        "has been received",
                        "acknowledgment",
                    ]

                    page_text_lower = page_text.lower()
                    is_success = any(p in page_text_lower for p in success_patterns)

                    final_url = await get_page_url(self._page)
                    if (
                        "confirmation" in final_url.lower()
                        or "orderack" in final_url.lower()
                    ):
                        is_success = True

                    if is_success:
                        action_str = "Bought" if side == "buy" else "Sold"
                        broker_event(
                            f"✓ {action_str} {qty} shares of {ticker} on {account_name}",
                            logger=logger,
                        )
                        success_count += 1
                    else:
                        broker_event(
                            f"Order may have failed on {account_name} - no clear success confirmation",
                            level="warning",
                            logger=logger,
                        )
                        logger.debug("Final URL: %s", final_url)

                        errors_on_page = await self._page.evaluate("""
                            Array.from(document.querySelectorAll('.error, .error-message, [class*="error"], [class*="Error"]'))
                            .map(el => el.textContent.trim())
                            .filter(text => text.length > 0 && text.includes('Error'))
                            .slice(0, 2)
                        """)
                        if errors_on_page:
                            for error in errors_on_page:
                                clean_error = " ".join(error.split())
                                broker_event(f"  ⚠ {clean_error}", level="warning", logger=logger)
                except Exception as e:
                    logger.warning("Could not verify order success: %s", e)

            except Exception as e:
                broker_event(
                    f"Error trading on {account_name}: {e}",
                    level="error",
                    logger=logger,
                    exc=e,
                )

                continue

        # Return True if at least one account succeeded, False if all failed
        return success_count > 0


async def wellsfargoGetHoldings(ticker=None):
    """Get holdings from all Wells Fargo accounts."""
    await rate_limiter.wait_if_needed("WellsFargo")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("WellsFargo")
    if not session:
        broker_event(
            "No Wells Fargo credentials supplied, skipping",
            level="warning",
            logger=logger,
        )
        return None

    # Create client and use it via async context manager
    # Default to visible browser (headless=false) for Wells Fargo since CAPTCHAs are common
    headless = os.getenv("HEADLESS", "false").lower() == "true"
    try:
        async with WellsFargoClient(
            username=session["username"],
            password=session["password"],
            phone_suffix=session.get("phone_suffix", ""),
            headless=headless,
        ) as client:
            return await client.get_holdings(ticker)
    except Exception as e:
        broker_event(
            f"Error getting Wells Fargo holdings: {e}",
            level="error",
            logger=logger,
            exc=e,
        )

        return None


async def wellsfargoTrade(side, qty, ticker, price):
    """Execute a trade on Wells Fargo Advisors.

    Returns:
        True: Trade executed successfully on at least one account
        False: Trade failed on all accounts
        None: No credentials (broker skipped)
    """
    await rate_limiter.wait_if_needed("WellsFargo")

    from brokers.session_manager import session_manager

    session = await session_manager.get_session("WellsFargo")
    if not session:
        broker_event(
            "No Wells Fargo credentials supplied, skipping",
            level="warning",
            logger=logger,
        )
        return None

    # Create client and use it via async context manager
    # Default to visible browser (headless=false) for Wells Fargo since CAPTCHAs are common
    headless = os.getenv("HEADLESS", "false").lower() == "true"
    try:
        async with WellsFargoClient(
            username=session["username"],
            password=session["password"],
            phone_suffix=session.get("phone_suffix", ""),
            headless=headless,
        ) as client:
            return await client.trade(side, qty, ticker, price)
    except Exception as e:
        broker_event(
            f"Error during Wells Fargo trade: {e}",
            level="error",
            logger=logger,
            exc=e,
        )

        return False


async def get_wellsfargo_session(session_manager):
    """Get or create Wells Fargo session."""
    if "wellsfargo" not in session_manager._initialized:
        WELLSFARGO_USER = os.getenv("WELLSFARGO_USER")
        WELLSFARGO_PASS = os.getenv("WELLSFARGO_PASS")
        WELLSFARGO_PHONE = os.getenv(
            "WELLSFARGO_PHONE_SUFFIX"
        )  # Optional phone suffix for 2FA

        if not (WELLSFARGO_USER and WELLSFARGO_PASS):
            session_manager.sessions["wellsfargo"] = None
            session_manager._initialized.add("wellsfargo")
            return None

        try:
            # Store credentials for later use by trade/holdings functions
            wellsfargo_session = {
                "username": WELLSFARGO_USER,
                "password": WELLSFARGO_PASS,
                "phone_suffix": WELLSFARGO_PHONE,
            }

            session_manager.sessions["wellsfargo"] = wellsfargo_session
            broker_event("✓ Wells Fargo credentials loaded", logger=logger)
        except Exception as e:
            broker_event(
                f"✗ Failed to initialize Wells Fargo session: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            session_manager.sessions["wellsfargo"] = None

        session_manager._initialized.add("wellsfargo")

    return session_manager.sessions.get("wellsfargo")
