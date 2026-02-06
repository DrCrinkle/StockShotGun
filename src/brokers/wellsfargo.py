"""Wells Fargo Advisors broker integration using zendriver."""

import os
import re
import asyncio
import logging
import traceback
from bs4 import BeautifulSoup
from zendriver import Browser
from brokers.base import rate_limiter, broker_event

logger = logging.getLogger(__name__)


class WellsFargoClient:
    """Wells Fargo Advisors client with browser automation."""

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
        self._accounts = None
        self._is_authenticated = False
        self._x_param = ""

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
        """Authenticate with Wells Fargo using browser automation."""
        try:
            # Start browser
            browser_args = [
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
            ]

            self._browser = await Browser.create(
                browser_args=browser_args, headless=self._headless
            )

            # Navigate to Wells Fargo Advisors login
            self._page = await self._browser.get("https://www.wellsfargoadvisors.com/")
            page = self._page
            await asyncio.sleep(2)

            # print(f"[DEBUG] Initial page loaded, URL: {page.url}")

            # Enter username (exact selector from reference)
            username_input = await page.select("input[id=j_username]")
            await username_input.click()
            await username_input.clear_input()
            await username_input.send_keys(self._username)

            # Enter password (exact selector from reference)
            password_input = await page.select("input[id=j_password]")
            await password_input.send_keys(self._password)

            # Click sign on button using exact class selector from reference
            sign_on_button = await page.select(".button.button--login.button--signOn")
            await sign_on_button.click()

            # print(f"[DEBUG] Sign on button clicked")
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

                try:
                    current_url = await page.evaluate("window.location.href")
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    current_url = page.url

                try:
                    page_title = await page.evaluate("document.title")
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    page_title = ""

                url_lower = (current_url or "").lower()
                title_lower = page_title.lower()

                # print(f"[DEBUG] Attempt {attempt + 1}/12 - URL: {current_url[:80]}... | Title: {page_title[:50]}...")

                # Check for error in URL
                if "error=yes" in url_lower:
                    # print(f"[DEBUG] Login error detected in URL!")
                    pass

                if any(marker in url_lower for marker in success_url_markers) or any(
                    marker in title_lower for marker in success_title_markers
                ):
                    if not login_verified:
                        broker_event("Successfully logged in!", logger=logger)
                    login_verified = True
                    break

                if "interdiction" in url_lower:
                    needs_additional_verification = True
                    # print(f"[DEBUG] 2FA required (interdiction detected)")
                    break

            if needs_additional_verification and not login_verified:
                broker_event(
                    "Login requires additional Wells Fargo verification (e.g. 2FA).",
                    level="warning",
                    logger=logger,
                )

            # Check if we got an error (anti-bot check)
            if not login_verified and not needs_additional_verification:
                # Check for anti-bot challenge
                if "error=yes" in current_url.lower() or "login" in current_url.lower():
                    broker_event(f"\n{'=' * 60}", logger=logger)
                    broker_event(
                        "⚠️  Anti-bot challenge detected!",
                        level="warning",
                        logger=logger,
                    )
                    broker_event(
                        f"Current URL: {current_url}",
                        level="warning",
                        logger=logger,
                    )
                    broker_event(
                        f"Page title: {page_title}",
                        level="warning",
                        logger=logger,
                    )
                    broker_event("\nPlease check the browser window:", logger=logger)
                    broker_event(
                        "  1. Solve any CAPTCHA/puzzle if shown", logger=logger
                    )
                    broker_event(
                        "  2. Log in manually after solving the puzzle", logger=logger
                    )
                    broker_event("\nWaiting for login to complete...", logger=logger)
                    broker_event(f"{'=' * 60}\n", logger=logger)
                else:
                    # Still haven't navigated - likely a puzzle or extra verification
                    broker_event(f"Current URL: {current_url}", logger=logger)
                    broker_event(f"Page title: {page_title}", logger=logger)

                    broker_event(f"\n{'=' * 60}", logger=logger)
                    broker_event(
                        "⚠️  Login not completing - likely a CAPTCHA/Puzzle!",
                        level="warning",
                        logger=logger,
                    )
                    broker_event(
                        "Please check the browser window and log in manually",
                        level="warning",
                        logger=logger,
                    )
                    broker_event("\nWaiting for login to complete...", logger=logger)
                    broker_event(f"{'=' * 60}\n", logger=logger)

                # Poll for successful login (up to 2 minutes)
                broker_event("Polling for successful login...", logger=logger)
                for poll_attempt in range(60):
                    await asyncio.sleep(2)
                    try:
                        poll_url = await page.evaluate("window.location.href")
                        poll_title = await page.evaluate("document.title")

                        # Check if we successfully logged in
                        if (
                            "wellstrade" in poll_url.lower()
                            or "brokoverview" in poll_url.lower()
                        ) and any(
                            keyword in poll_title.lower()
                            for keyword in ["brokerage overview", "wellstrade"]
                        ):
                            broker_event("✓ Login successful!", logger=logger)
                            login_verified = True
                            break
                    except Exception as exc:
                        logger.debug(
                            "Ignored transient error while polling Wells Fargo login state",
                            exc_info=exc,
                        )

            # Handle 2FA if needed (check for INTERDICTION in URL as per reference)
            try:
                current_url = await page.evaluate("window.location.href")
            except Exception as exc:
                logger.debug(
                    "Could not read Wells Fargo current URL via page.evaluate",
                    exc_info=exc,
                )
                current_url = page.url

            if "dest=INTERDICTION" in current_url:
                broker_event(f"\n{'=' * 60}", logger=logger)
                broker_event(
                    "⚠️  Wells Fargo 2FA required!", level="warning", logger=logger
                )
                broker_event(
                    "Please complete 2FA in the browser window:", logger=logger
                )
                broker_event(
                    "  - Select your 2FA method (SMS, email, etc.)", logger=logger
                )
                broker_event("  - Enter the code when prompted", logger=logger)
                broker_event("  - Click submit", logger=logger)
                broker_event("\nWaiting for 2FA to complete...", logger=logger)
                broker_event(f"{'=' * 60}\n", logger=logger)

                # Poll for 2FA completion (up to 120 seconds)
                for attempt in range(60):
                    await asyncio.sleep(2)
                    try:
                        current_url = await page.evaluate("window.location.href")
                        page_title = await page.evaluate("document.title")

                        # Check if we've moved past 2FA
                        if "interdiction" not in current_url.lower():
                            broker_event("✓ 2FA completed successfully", logger=logger)
                            break
                    except Exception as exc:
                        logger.debug(
                            "Ignored transient error while polling Wells Fargo 2FA state",
                            exc_info=exc,
                        )

            # Verify login was successful
            # print(f"[DEBUG] Verifying login, login_verified={login_verified}")
            try:
                current_url = await page.evaluate("window.location.href")
                page_title = await page.evaluate("document.title")
            except Exception as exc:
                logger.debug(
                    "Could not read Wells Fargo URL/title during login verification",
                    exc_info=exc,
                )
                current_url = page.url
                page_title = ""

            # print(f"[DEBUG] Final verification - URL: {current_url[:80]}... | Title: {page_title[:50]}...")

            # Check if we're already on the overview/accounts page
            already_on_overview = any(
                keyword in current_url.lower()
                for keyword in ["brokoverview", "wellstrade"]
            ) or any(
                keyword in page_title.lower()
                for keyword in ["brokerage overview", "wellstrade"]
            )

            # print(f"[DEBUG] already_on_overview: {already_on_overview}")

            if "login" in current_url.lower() and not already_on_overview:
                broker_event(
                    "✗ Wells Fargo login failed - still on login page",
                    level="error",
                    logger=logger,
                )
                broker_event(
                    f"Current URL: {current_url}",
                    level="warning",
                    logger=logger,
                )
                raise Exception("Wells Fargo login failed")

            # Navigate to accounts overview page to enable account discovery
            # This ensures we have the accounts table available
            broker_event(
                "Login successful! Loading account information...", logger=logger
            )

            if not already_on_overview:
                # print(f"[DEBUG] Navigating to brokoverview page...")
                try:
                    await page.get(
                        "https://wfawellstrade.wellsfargo.com/BW/brokoverview.do"
                    )
                    await asyncio.sleep(3)
                    # print(f"[DEBUG] After navigation, URL: {await page.evaluate('window.location.href') if page else 'unknown'}")
                except Exception as e:
                    broker_event(
                        f"Warning: Could not navigate to overview page: {e}",
                        level="warning",
                        logger=logger,
                        exc=e,
                    )
            else:
                # print(f"[DEBUG] Already on accounts overview page")
                pass

            broker_event("✓ Wells Fargo authenticated successfully", logger=logger)
            self._page = page
            self._is_authenticated = True
            # print(f"[DEBUG] self._page set: {self._page is not None}, self._is_authenticated: {self._is_authenticated}")

        except Exception as e:
            broker_event(
                f"Error during Wells Fargo authentication: {e}",
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

    async def _extract_x_param(self):
        """Extract the dynamic _x parameter from current URL using regex."""
        try:
            # Use JavaScript to get real URL since page.url may be stuck at about:blank
            try:
                current_url = await self._page.evaluate("window.location.href")
            except Exception as exc:
                logger.debug(
                    "Could not read Wells Fargo URL while extracting _x parameter",
                    exc_info=exc,
                )
                current_url = self._page.url

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
            try:
                current_url = await self._page.evaluate("window.location.href")
            except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                current_url = self._page.url

            if "brokoverview" not in (current_url or "").lower():
                try:
                    await self._page.get(
                        "https://wfawellstrade.wellsfargo.com/BW/brokoverview.t.do"
                    )
                    await asyncio.sleep(3)
                except Exception as nav_err:
                    print(
                        f"Warning: Unable to navigate to Wells Fargo accounts overview: {nav_err}"
                    )
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

            broker_event(
                f"Found {len(account_rows)} account rows in HTML", logger=logger
            )

            # Debug: also check for any table rows
            all_table_rows = soup.select("table tr")
            broker_event(
                f"Total table rows on page: {len(all_table_rows)}", logger=logger
            )

            accounts = []
            account_index = 0  # Separate counter for non-"-1" accounts
            for idx, row in enumerate(account_rows):
                # Skip "All Accounts" row (data-p_account="-1")
                account_attr = row.get("data-p_account")
                if account_attr == "-1":
                    print(
                        f"Skipping 'All Accounts' row (data-p_account={account_attr})"
                    )
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
            print(f"Error discovering accounts: {e}")
            traceback.print_exc()
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
                print(f"Error parsing holdings row: {e}")
                continue

        return holdings

    async def _current_url(self):
        """Get current URL safely via JavaScript evaluation."""
        try:
            return await self._page.evaluate("window.location.href")
        except Exception as exc:
            logger.debug(
                "Could not read Wells Fargo current URL via page.evaluate",
                exc_info=exc,
            )
            return self._page.url

    async def _page_title(self):
        """Get page title safely via JavaScript evaluation."""
        try:
            return await self._page.evaluate("document.title")
        except Exception as exc:
            logger.debug(
                "Could not read Wells Fargo page title via page.evaluate",
                exc_info=exc,
            )
            return ""

    async def _goto_holdings(self, account_index, x_param):
        """Navigate to holdings page for the specified account."""
        holdings_url = f"https://wfawellstrade.wellsfargo.com/BW/holdings.do?account={account_index}"
        if x_param:
            holdings_url += f"&{x_param}"

        print(f"  Navigating to: {holdings_url[:120]}...")
        await self._page.get(holdings_url)
        await asyncio.sleep(3)

    async def _goto_trade_form(self, account_param, ticker, side, x_param):
        """Navigate to trade form for the specified account and ticker."""
        action_value = "BUY" if side == "buy" else "SELL"
        trade_url = f"https://wfawellstrade.wellsfargo.com/BW/equity.do?account={account_param}&symbol={ticker}&selectedAction={action_value}"
        if x_param:
            trade_url += f"&{x_param}"

        print(f"Navigating to trade URL: {trade_url}")
        await self._page.get(trade_url)
        await asyncio.sleep(3)

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

            print(f"\nFetching holdings for: {account_name}")

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
                    print(f"  ⚠️ Error page detected for {account_name}")
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
                traceback.print_exc()
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

        # Discover all accounts
        print("Discovering Wells Fargo accounts...")
        accounts = await self._discover_accounts()
        print(f"Found {len(accounts)} Wells Fargo account(s)")

        success_count = 0

        # Trade on all accounts
        for account in accounts:
            account_name = account["name"]
            # Use data_p_account if available, otherwise fall back to index
            account_param = account.get("data_p_account", account["index"])
            x_param = account["x_param"]

            # Reset price for this account (use per-account copy to avoid stale pricing)
            price_for_account = user_specified_price

            print(f"\nTrading on: {account_name} (account param: {account_param})")

            try:
                # Navigate to trade page for this account WITH symbol pre-filled
                await self._goto_trade_form(account_param, ticker, side, x_param)

                # Debug: Check if we're on the right page
                current_url = await self._current_url()
                print(f"Current URL after navigation: {current_url}")

                # Wait for the page body to be fully rendered
                await self._page.wait_for("body", timeout=10)

                # Set buy/sell action by directly manipulating form
                print("Setting buy/sell action...")
                try:
                    action_text = "Buy" if side == "buy" else "Sell"
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
                        print(f"Set action to {action_text}")
                        await asyncio.sleep(0.5)
                    else:
                        print("Warning: Could not find BuySell elements")
                except Exception as e:
                    print(f"Warning: Could not set BuySell: {e}")

                # Verify quote loaded
                print("Checking if quote loaded...")
                try:
                    await asyncio.sleep(2)
                    quote_loaded = await self._page.evaluate(
                        "document.getElementById('last')?.value"
                    )
                    if quote_loaded and quote_loaded != "None" and quote_loaded.strip():
                        print(f"Quote loaded: ${quote_loaded}")
                    else:
                        print(
                            f"Warning: Quote may not have loaded (value: '{quote_loaded}'), will use default pricing"
                        )
                except Exception as e:
                    print(f"Error checking quote: {e}")

                # Enter quantity
                print("Setting quantity...")
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
                        print(f"Quantity set to {result}")
                        await asyncio.sleep(0.5)
                    else:
                        print("Warning: Could not find OrderQuantity input field")
                        continue
                except Exception as e:
                    print(f"Error setting quantity: {e}")
                    continue

                # Get last price from page for limit orders
                if not price_for_account:
                    try:
                        last_price_str = await self._page.evaluate(
                            "document.getElementById('last')?.value"
                        )
                        print(f"Retrieved last price: {last_price_str}")

                        if last_price_str and last_price_str.strip():
                            price_for_account = float(last_price_str.strip())
                            print(f"Current stock price: ${price_for_account}")
                        else:
                            print(
                                "Could not get price from page, defaulting to $1 (will use limit order)"
                            )
                            price_for_account = 1.00
                    except Exception as e:
                        print(f"Error getting price: {e}")
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
                    print(f"Setting order type to LIMIT ({limit_reason})...")
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
                            print("Set order type to Limit")
                    except Exception as e:
                        print(f"Warning setting order type: {e}")

                    await asyncio.sleep(0.5)

                    # Calculate limit price
                    if user_specified_price:
                        adjusted_price = round(user_specified_price, 2)
                        print(f"Using user-specified price ${adjusted_price}")
                    else:
                        if side == "buy":
                            adjusted_price = round(price_for_account + 0.01, 2)
                            print(
                                f"Using last price ${price_for_account} + $0.01 for buy"
                            )
                        else:
                            adjusted_price = round(price_for_account - 0.01, 2)
                            print(
                                f"Using last price ${price_for_account} - $0.01 for sell"
                            )

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
                            print(f"Limit price set to ${adjusted_price}")
                    except Exception as e:
                        print(f"Warning setting limit price: {e}")
                else:
                    print("Setting order type to MARKET...")
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
                            print("Set order type to Market")
                            await asyncio.sleep(0.5)
                    except Exception as e:
                        print(f"Warning setting order type: {e}")

                # Set time in force to Day
                print("Setting time in force to DAY...")
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
                        print("Set TIF to Day")
                        await asyncio.sleep(0.5)
                except Exception as e:
                    print(f"Warning setting TIF: {e}")

                await asyncio.sleep(1)

                # Click continue button
                print("Clicking continue button...")
                continue_button = await self._page.select(
                    "button[id=actionbtnContinue]", timeout=5
                )
                if continue_button:
                    await continue_button.click()
                    await asyncio.sleep(3)
                else:
                    print("Warning: Could not find continue button")
                    continue

                # Check preview page
                print("Checking order preview page...")
                try:
                    page_title = await self._page_title()
                    current_url = await self._current_url()
                    print(f"Preview page title: {page_title}")
                    print(f"Preview URL: {current_url}")
                except Exception as exc:
                    logger.debug(
                        "Ignored transient error while reading Wells Fargo preview metadata",
                        exc_info=exc,
                    )

                # Check for errors
                try:
                    error_texts = await self._page.evaluate("""
                        Array.from(document.querySelectorAll('body')).map(el => el.textContent).join('\\n')
                    """)

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
                        print(f"Wells Fargo order errors for {account_name}:")
                        for err in error_messages:
                            print(f"  - {err}")
                        continue
                    else:
                        print("Order preview looks good (warnings are OK)")
                except Exception as ex:
                    print(f"Error checking for validation errors: {ex}")

                # Find confirm button
                confirm_button = None
                try:
                    confirm_button = await self._page.select(
                        ".btn-wfa-primary.btn-wfa-submit", timeout=5
                    )
                    if confirm_button:
                        print("Found submit button (.btn-wfa-primary.btn-wfa-submit)")
                except Exception as exc:
                    logger.debug(
                        "Could not find primary Wells Fargo submit button selector",
                        exc_info=exc,
                    )
                    for button_id in ["actionbtnContinue", "confirmBtn", "submitBtn"]:
                        try:
                            confirm_button = await self._page.select(
                                f"button[id={button_id}]", timeout=2
                            )
                            if confirm_button:
                                print(f"Found confirm button: {button_id}")
                                break
                        except (
                            asyncio.TimeoutError,
                            AttributeError,
                            RuntimeError,
                            TypeError,
                        ):
                            continue

                if not confirm_button:
                    print(
                        f"Wells Fargo order cannot be placed on {account_name} - no confirmation button found"
                    )
                    try:
                        buttons = await self._page.evaluate("""
                            Array.from(document.querySelectorAll('button')).map(btn => ({
                                id: btn.id,
                                class: btn.className,
                                text: btn.textContent.trim().substring(0, 30)
                            })).filter(b => b.id || b.text)
                        """)
                        print(f"Available buttons: {buttons[:5]}")
                    except Exception as exc:
                        logger.debug(
                            "Ignored transient error while reading Wells Fargo submit button diagnostics",
                            exc_info=exc,
                        )
                    continue

                # Check dry-run
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    print(
                        f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}"
                    )
                    success_count += 1
                    continue

                # Submit order
                print("Submitting order...")
                await confirm_button.click()
                await asyncio.sleep(3)

                # Check for success
                try:
                    final_title = await self._page_title()
                    print(f"After submission - Title: {final_title}")

                    page_text = await self._page.evaluate("document.body.textContent")

                    success_patterns = [
                        "order has been placed",
                        "successfully",
                        "Success",
                        "confirmed",
                        "Confirmed",
                        "Order Number",
                        "order number",
                        "has been received",
                        "Acknowledgment",
                    ]

                    is_success = any(
                        pattern.lower() in page_text.lower()
                        for pattern in success_patterns
                    )

                    final_url = await self._current_url()
                    if (
                        "confirmation" in final_url.lower()
                        or "orderack" in final_url.lower()
                    ):
                        is_success = True

                    if is_success:
                        action_str = "Bought" if side == "buy" else "Sold"
                        print(
                            f"✓ {action_str} {qty} shares of {ticker} on {account_name}"
                        )
                        success_count += 1
                    else:
                        print(
                            f"Wells Fargo order may have failed on {account_name} - no clear success confirmation"
                        )
                        print(f"Final URL: {final_url}")

                        errors_on_page = await self._page.evaluate("""
                            Array.from(document.querySelectorAll('.error, .error-message, [class*="error"], [class*="Error"]'))
                            .map(el => el.textContent.trim())
                            .filter(text => text.length > 0 && text.includes('Error'))
                            .slice(0, 2)
                        """)
                        if errors_on_page:
                            for error in errors_on_page:
                                clean_error = " ".join(error.split())
                                print(f"  ⚠ {clean_error}")
                except Exception as e:
                    print(f"Could not verify order success: {e}")

            except Exception as e:
                broker_event(
                    f"Error trading on {account_name}: {e}",
                    level="error",
                    logger=logger,
                    exc=e,
                )
                traceback.print_exc()
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
        traceback.print_exc()
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
        traceback.print_exc()
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
