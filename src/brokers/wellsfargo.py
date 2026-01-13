"""Wells Fargo Advisors broker integration using zendriver."""

import os
import re
import asyncio
import traceback
from bs4 import BeautifulSoup
from zendriver import Browser, SpecialKeys
from brokers.base import rate_limiter


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
        self._account_indices = {}

    async def __aenter__(self):
        """Async context manager entry. Browser created lazily on first operation."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit. Cleanup browser if exists."""
        if self._browser:
            try:
                await self._browser.stop()
            except Exception as e:
                print(f"Error stopping browser: {e}")
            finally:
                self._browser = None
                self._page = None
                self._is_authenticated = False

    async def _take_screenshot_on_error(self, message):
        try:
            if self._page:
                import datetime

                timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                screenshot_name = f"wells-fargo-error-{timestamp}.png"
                await self._page.save_screenshot(filename=screenshot_name)
                print(f"Screenshot saved: {screenshot_name}")
        except Exception as e:
            print(f"Failed to take screenshot: {e}")

    async def _select_dropdown_option(
        self, dropdown_selector, option_value, timeout=10
    ):
        try:
            await self._page.wait_for_ready_state("complete")
            opener = await self._page.select(dropdown_selector, timeout=timeout)
            await opener.scroll_into_view()
            await opener.mouse_click()
            await asyncio.sleep(0.5)

            option_selector = f"a[data-val='{option_value}']"
            option = await self._page.select(option_selector, timeout=timeout)
            await option.scroll_into_view()
            await option.mouse_click()
            await asyncio.sleep(0.5)
            return True
        except Exception as e:
            print(
                f"Failed to select '{option_value}' from dropdown '{dropdown_selector}': {e}"
            )
            return False

    async def _ensure_authenticated(self):
        """Ensure browser is authenticated. Lazy authentication - only creates browser when needed."""
        if not self._is_authenticated or not self._browser:
            await self._authenticate()

    async def _authenticate(self):
        """Authenticate with Wells Fargo using browser automation."""
        try:
            browser_args = [
                "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "--no-first-run",
                "--disable-default-apps",
            ]

            profile_dir = os.path.join(
                os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                ),
                "tokens",
                "wellsfargo_profile",
            )
            if not os.path.exists(profile_dir):
                os.makedirs(profile_dir)

            self._browser = await Browser.create(
                browser_args=browser_args,
                headless=self._headless,
                user_data_dir=profile_dir,
            )

            # Navigate to Wells Fargo Advisors login
            self._page = await self._browser.get(
                "https://www.wellsfargoadvisors.com/online-access/signon.htm"
            )
            page = self._page
            await page.wait_for_ready_state("complete", timeout=20)
            await asyncio.sleep(1)

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

            print("Waiting for login to process...")

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
                except Exception:
                    current_url = page.url

                try:
                    page_title = await page.evaluate("document.title")
                except Exception:
                    page_title = ""

                url_lower = (current_url or "").lower()
                title_lower = page_title.lower()

                # Check for error in URL
                if "error=yes" in url_lower:
                    pass

                if any(marker in url_lower for marker in success_url_markers) or any(
                    marker in title_lower for marker in success_title_markers
                ):
                    if not login_verified:
                        print("Successfully logged in!")
                    login_verified = True
                    break

                if "interdiction" in url_lower:
                    needs_additional_verification = True
                    break

            if needs_additional_verification and not login_verified:
                print("Login requires additional Wells Fargo verification (e.g. 2FA).")

            # Check if we got an error (anti-bot check)
            if not login_verified and not needs_additional_verification:
                # Check for anti-bot challenge
                if "error=yes" in current_url.lower() or "login" in current_url.lower():
                    print(f"\n{'=' * 60}")
                    print("⚠️  Anti-bot challenge detected!")
                    print("Current URL:", current_url)
                    print("Page title:", page_title)
                    print("\nPlease check the browser window:")
                    print("  1. Solve any CAPTCHA/puzzle if shown")
                    print("  2. Log in manually after solving the puzzle")
                    print("\nWaiting for login to complete...")
                    print(f"{'=' * 60}\n")
                else:
                    # Still haven't navigated - likely a puzzle or extra verification
                    print(f"Current URL: {current_url}")
                    print(f"Page title: {page_title}")

                    print(f"\n{'=' * 60}")
                    print("⚠️  Login not completing - likely a CAPTCHA/Puzzle!")
                    print("Please check the browser window and log in manually")
                    print("\nWaiting for login to complete...")
                    print(f"{'=' * 60}\n")

                # Poll for successful login (up to 2 minutes)
                print("Polling for successful login...")
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
                            print("✓ Login successful!")
                            login_verified = True
                            break
                    except Exception:
                        pass

            # Handle 2FA if needed (check for INTERDICTION in URL as per reference)
            try:
                current_url = await page.evaluate("window.location.href")
            except:
                current_url = page.url

            if "dest=INTERDICTION" in current_url:
                print(f"\n{'=' * 60}")
                print("⚠️  Wells Fargo 2FA required!")

                try:
                    await page.wait_for_ready_state("complete")
                    await asyncio.sleep(1)
                    content = await page.get_content()

                    if "We sent a notification to your phone" in content:
                        print(
                            "Push notification detected. Please approve on your mobile device."
                        )
                        print("Waiting up to 2 minutes...")

                        for _ in range(60):
                            await asyncio.sleep(2)
                            current_url = await page.evaluate("window.location.href")
                            if "brokoverview" in current_url:
                                print("✓ Push notification approved.")
                                login_verified = True
                                break

                        if not login_verified:
                            print(
                                "Push notification timed out. Trying alternate method..."
                            )
                            try:
                                try_another_method_btn = await page.select(
                                    "#buttonTryAnotherMethod", timeout=10
                                )
                                if try_another_method_btn:
                                    await try_another_method_btn.scroll_into_view()
                                    await try_another_method_btn.mouse_click()
                                    await page.wait_for_ready_state("complete")
                                    await asyncio.sleep(2)
                            except Exception:
                                pass

                    if not login_verified:
                        try:
                            contact_options = await page.select_all(
                                '[role="listitem"] button', timeout=5
                            )
                            if contact_options:
                                for option in contact_options:
                                    option_text = (
                                        option.text_all
                                        if hasattr(option, "text_all")
                                        else ""
                                    )
                                    if "Mobile" in option_text:
                                        print("Found 'Mobile' option. Clicking it.")
                                        await option.mouse_click()
                                        await page.wait_for_ready_state("complete")
                                        await asyncio.sleep(1)
                                        break
                        except Exception:
                            pass

                        try:
                            text_me_btn = await page.select(
                                "#optionSMS button", timeout=5
                            )
                            if text_me_btn:
                                await text_me_btn.mouse_click()
                                print("Clicked 'Text me a code'. Waiting for OTP page.")
                                await page.wait_for_ready_state("complete")
                                await asyncio.sleep(2)
                        except Exception:
                            pass

                        try:
                            otp_input = await page.select("#otp", timeout=5)
                            if otp_input:
                                print("\n!!! WELLS FARGO OTP REQUIRED !!!")
                                otp_code = (
                                    await asyncio.get_event_loop().run_in_executor(
                                        None,
                                        input,
                                        "Please enter Wells Fargo OTP code: ",
                                    )
                                )
                                if otp_code:
                                    await otp_input.send_keys(otp_code)
                                    continue_btn = await page.select(
                                        'button[type="submit"]', timeout=10
                                    )
                                    if continue_btn:
                                        await continue_btn.mouse_click()
                                        print("OTP submitted.")
                                        await page.wait_for_ready_state("complete")
                                        await asyncio.sleep(2)
                        except Exception:
                            pass

                except Exception as e:
                    print(f"Error during 2FA handling: {e}")

                if not login_verified:
                    print("\nPlease complete 2FA in the browser window if needed...")
                    print(f"{'=' * 60}\n")

                for attempt in range(60):
                    await asyncio.sleep(2)
                    try:
                        current_url = await page.evaluate("window.location.href")

                        if "interdiction" not in current_url.lower():
                            print("✓ 2FA completed successfully")
                            login_verified = True
                            break
                    except Exception:
                        pass

            # Verify login was successful
            try:
                current_url = await page.evaluate("window.location.href")
                page_title = await page.evaluate("document.title")
            except:
                current_url = page.url
                page_title = ""

            # Check if we're already on the overview/accounts page
            already_on_overview = any(
                keyword in current_url.lower()
                for keyword in ["brokoverview", "wellstrade"]
            ) or any(
                keyword in page_title.lower()
                for keyword in ["brokerage overview", "wellstrade"]
            )

            if "login" in current_url.lower() and not already_on_overview:
                print("✗ Wells Fargo login failed - still on login page")
                print(f"Current URL: {current_url}")
                raise Exception("Wells Fargo login failed")

            # Navigate to accounts overview page to enable account discovery
            # This ensures we have the accounts table available
            print("Login successful! Loading account information...")

            if not already_on_overview:
                try:
                    await page.get(
                        "https://wfawellstrade.wellsfargo.com/BW/brokoverview.do"
                    )
                    await asyncio.sleep(3)
                except Exception as e:
                    print(f"Warning: Could not navigate to overview page: {e}")

            print("✓ Wells Fargo authenticated successfully")
            self._page = page
            self._is_authenticated = True

        except Exception as e:
            print(f"Error during Wells Fargo authentication: {e}")
            traceback.print_exc()
            await self._take_screenshot_on_error(f"Authentication error: {e}")
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
            except:
                current_url = self._page.url

            match = re.search(r"_x=([^&]+)", current_url)
            if match:
                return f"_x={match.group(1)}"
            return ""
        except Exception as e:
            print(f"Error extracting x_param: {e}")
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
            except Exception:
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
                except Exception:
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
            print(f"Extracted x_param: {x_param[:50] if x_param else 'None'}...")

            # If still no rows found, fall back to parsing whatever content is available
            if not account_rows:
                html = await self._page.get_content()
                soup = BeautifulSoup(html, "html.parser")
                account_rows = soup.select("tr[data-p_account]")

            print(f"Found {len(account_rows)} account rows in HTML")

            # Debug: also check for any table rows
            all_table_rows = soup.select("table tr")
            print(f"Total table rows on page: {len(all_table_rows)}")

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

                    account_key = (
                        f"{account_name} {account_number}"
                        if account_number
                        else account_name
                    )
                    accounts.append(
                        {
                            "index": account_index,
                            "data_p_account": account_attr,
                            "name": account_name,
                            "number": account_number,
                            "balance": balance,
                            "x_param": x_param,
                        }
                    )
                    self._account_indices[account_key] = {
                        "index": account_index,
                        "x_param": x_param,
                    }

                    print(
                        f"Found account #{account_index}: {account_name} ({account_number}) - ${balance:,.2f} [data-p_account={account_attr}]"
                    )
                    account_index += 1

                except Exception as e:
                    print(f"Error parsing account row {idx}: {e}")
                    continue

            if not accounts:
                print("Warning: No accounts found on page")
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
        except:
            return self._page.url

    async def _page_title(self):
        """Get page title safely via JavaScript evaluation."""
        try:
            return await self._page.evaluate("document.title")
        except:
            return ""

    async def _goto_holdings(self, account_index, x_param):
        """Navigate to holdings page for the specified account."""
        holdings_url = f"https://wfawellstrade.wellsfargo.com/BW/holdings.do?account={account_index}"
        if x_param:
            holdings_url += f"&{x_param}"

        print(f"  Navigating to: {holdings_url[:120]}...")
        await self._page.get(holdings_url)
        await self._page.wait_for_ready_state("complete", timeout=20)
        await asyncio.sleep(1)

    async def _goto_trade_form(self, account_param, x_param):
        """Navigate to trade form for the specified account."""
        trade_url = f"https://wfawellstrade.wellsfargo.com/BW/equity.do?account={account_param}&symbol=&selectedAction="
        if x_param:
            trade_url += f"&{x_param}"

        print(f"Navigating to trade URL: {trade_url}")
        await self._page.get(trade_url)
        await self._page.wait_for_ready_state("complete", timeout=20)
        await asyncio.sleep(1)
        await self._page.select("#eqentryfrm", timeout=10)

    async def get_holdings(self, ticker=None):
        """Get holdings from all Wells Fargo accounts.

        Args:
            ticker: Optional ticker symbol to filter holdings

        Returns:
            Dictionary of account holdings or None if error
        """
        await self._ensure_authenticated()

        # Discover all accounts
        print("Discovering Wells Fargo accounts...")
        accounts = await self._discover_accounts()
        print(f"Found {len(accounts)} Wells Fargo account(s)")

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
                    print(f"Found {len(holdings)} position(s) in {account_name}")
                else:
                    print(f"No holdings found in {account_name}")

            except Exception as e:
                print(f"Error getting holdings for {account_name}: {e}")
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
                await self._goto_trade_form(account_param, x_param)

                print("Setting buy/sell action...")
                action_text = "Buy" if side == "buy" else "Sell"
                if await self._select_dropdown_option("#BuySellBtn", action_text):
                    print(f"Set action to {action_text}")
                else:
                    print(f"Error: Could not set BuySell to {action_text}, skipping order")
                    continue

                print(f"Entering symbol: {ticker}")
                symbol_input = await self._page.select("#Symbol", timeout=10)
                await symbol_input.scroll_into_view()
                await symbol_input.send_keys(ticker)
                await symbol_input.send_keys(SpecialKeys.TAB)

                print("Waiting for quote to load...")
                await self._page.select("#prevdata", timeout=10)

                if side == "sell":
                    try:
                        shares_element = await self._page.select(
                            "#currentSharesOwned .numshares", timeout=5
                        )
                        if shares_element:
                            owned_shares_text = shares_element.text_all.strip()
                            owned_shares_digits = re.sub(r"[^\d]", "", owned_shares_text)
                            if not owned_shares_digits:
                                print(
                                    f"Warning: Could not parse owned shares from '{owned_shares_text}', skipping order"
                                )
                                continue
                            owned_shares = int(owned_shares_digits)
                            print(f"Account owns {owned_shares} shares of {ticker}")

                            if owned_shares == 0:
                                print(f"Skipping {ticker}: You own 0 shares")
                                continue

                            if qty > owned_shares:
                                print(
                                    f"Skipping {ticker}: Order quantity ({qty}) exceeds shares owned ({owned_shares})"
                                )
                                continue
                    except Exception as e:
                        print(f"Warning: Could not check shares owned: {e}")

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

                order_type = "Limit" if use_limit_order else "Market"
                print(f"Setting order type to {order_type.upper()}...")
                await self._select_dropdown_option("#OrderTypeBtn", order_type)

                if use_limit_order:
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

                    print(f"Entering limit price ${adjusted_price}...")
                    limit_input = await self._page.select("#Price", timeout=10)
                    await limit_input.scroll_into_view()
                    await limit_input.send_keys(str(adjusted_price))

                print("Setting time in force to DAY...")
                await self._select_dropdown_option("#TIFBtn", "Day")

                print("Clicking preview order button...")
                preview_button = await self._page.select(
                    "#actionbtnContinue", timeout=10
                )
                await preview_button.scroll_into_view()
                await preview_button.mouse_click()
                await self._page.wait_for_ready_state("complete", timeout=20)
                await asyncio.sleep(1)

                # Check preview page
                print("Checking order preview page...")
                try:
                    page_title = await self._page_title()
                    current_url = await self._current_url()
                    print(f"Preview page title: {page_title}")
                    print(f"Preview URL: {current_url}")
                except Exception:
                    pass

                try:
                    error_element = await self._page.select(
                        ".alert-msg-summary p", timeout=2
                    )
                    if error_element:
                        error_text = error_element.text_all.strip().replace("\n", " ")
                        if "Error:" in error_text or error_text.startswith("Error"):
                            print(f"Wells Fargo HARD Error for {ticker}: {error_text}")
                            continue
                        else:
                            print(f"Wells Fargo Warning for {ticker}: {error_text}")
                            print("Continuing with order submission...")
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
                except Exception:
                    for button_id in ["actionbtnContinue", "confirmBtn", "submitBtn"]:
                        try:
                            confirm_button = await self._page.select(
                                f"button[id={button_id}]", timeout=2
                            )
                            if confirm_button:
                                print(f"Found confirm button: {button_id}")
                                break
                        except Exception:
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
                    except Exception:
                        pass
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
                print(f"Error trading on {account_name}: {e}")
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
        print("No Wells Fargo credentials supplied, skipping")
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
        print(f"Error getting Wells Fargo holdings: {e}")
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
        print("No Wells Fargo credentials supplied, skipping")
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
        print(f"Error during Wells Fargo trade: {e}")
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
            print("✓ Wells Fargo credentials loaded")
        except Exception as e:
            print(f"✗ Failed to initialize Wells Fargo session: {e}")
            session_manager.sessions["wellsfargo"] = None

        session_manager._initialized.add("wellsfargo")

    return session_manager.sessions.get("wellsfargo")
