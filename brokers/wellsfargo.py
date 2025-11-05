"""Wells Fargo Advisors broker integration using zendriver."""

import os
import re
import asyncio
import traceback
from bs4 import BeautifulSoup
from zendriver import Browser


async def _wellsfargo_authenticate(session_info):
    """Authenticate with Wells Fargo using browser automation and return browser instance."""
    username = session_info["username"]
    password = session_info["password"]
    phone_suffix = session_info.get("phone_suffix", "")

    browser = None
    try:
        # Start browser
        browser_args = [
            "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
        ]
        headless = os.getenv("HEADLESS", "true").lower() == "true"

        browser = await Browser.create(
            browser_args=browser_args,
            headless=headless
        )

        # Navigate to Wells Fargo Advisors login
        page = await browser.get("https://www.wellsfargoadvisors.com/")
        await asyncio.sleep(2)

        # Enter username (exact selector from reference)
        username_input = await page.select("input[id=j_username]")
        await username_input.click()
        await username_input.clear_input()
        await username_input.send_keys(username)

        # Enter password (exact selector from reference)
        password_input = await page.select("input[id=j_password]")
        await password_input.send_keys(password)

        # Click sign on button using exact class selector from reference
        sign_on_button = await page.select(".button.button--login.button--signOn")
        await sign_on_button.click()

        print("Waiting for login to process...")

        # Wait and check multiple times for navigation
        has_puzzle = False
        login_verified = False
        needs_additional_verification = False
        current_url = page.url
        page_title = ""

        success_url_markers = ("wellstrade", "brokoverview")
        success_title_markers = ("brokerage overview", "wellstrade", "accounts")

        for attempt in range(12):  # Give the login a little longer before assuming puzzle
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

            if any(marker in url_lower for marker in success_url_markers) or \
               any(marker in title_lower for marker in success_title_markers):
                if not login_verified:
                    print("Successfully logged in!")
                login_verified = True
                break

            if "interdiction" in url_lower:
                needs_additional_verification = True
                break

        if needs_additional_verification and not login_verified:
            print("Login requires additional Wells Fargo verification (e.g. 2FA).")

        if not login_verified and not needs_additional_verification:
            # Still haven't navigated - likely a puzzle or extra verification
            print(f"Current URL: {current_url}")
            print(f"Page title: {page_title}")

            print(f"\n{'='*60}")
            print("⚠️  Login not completing - likely a CAPTCHA/Puzzle!")
            print("Please check the browser window:")
            print("  - If you see a puzzle, solve it")
            print("  - Wait for the page to completely reload (may take a few seconds)")
            print("Press ENTER ONLY after the page has fully loaded...")
            print(f"{'='*60}\n")
            input()
            print("Checking page status...")

            # Wait longer for page navigation and give more checks
            print("Waiting for page to navigate...")
            for wait_attempt in range(10):  # Wait up to 10 seconds
                await asyncio.sleep(1)
                # Use evaluate to get real URL since page.url may be stuck
                try:
                    real_url = await page.evaluate("window.location.href")
                    page_title = await page.evaluate("document.title")

                    # Check if we're already logged in (title contains these keywords)
                    if any(keyword in page_title.lower() for keyword in ["brokerage overview", "wellstrade", "accounts"]):
                        print(f"Already logged in! Page title: {page_title}")
                        print(f"Real URL: {real_url[:80]}...")
                        current_url = real_url
                        login_verified = True
                        break
                    elif real_url != "about:blank":
                        print(f"Page navigated to: {real_url[:80]}...")
                        current_url = real_url
                        break
                except Exception:
                    current_url = page.url

                if current_url != "about:blank":
                    break
                print(f"  Still at about:blank, waiting... ({wait_attempt + 1}/10)")

            await asyncio.sleep(2)  # Extra time for page to fully load
            has_puzzle = not login_verified

        # After puzzle handling, check if we need to re-enter credentials
        if has_puzzle:
            # Get real URL using JavaScript since page.url may be stuck
            try:
                current_url = await page.evaluate("window.location.href")
                page_title = await page.evaluate("document.title")
            except:
                current_url = page.url
                page_title = ""

            print(f"Post-puzzle URL: {current_url}")
            print(f"Page title: {page_title}")

            # Check if we're already logged in based on page title
            already_logged_in = any(keyword in page_title.lower() for keyword in ["brokerage overview", "wellstrade", "accounts overview"])

            if already_logged_in:
                print("✓ Already successfully logged in after puzzle!")
            else:
                # Check if we're back on a login page (could be different URLs)
                is_login_page = (
                    "login" in current_url.lower() or
                    "signon" in current_url.lower() or
                    "connect.secure.wellsfargo.com" in current_url.lower()
                )

                if is_login_page:
                    print("Detected login page - automatically re-entering credentials...")
                    await asyncio.sleep(2)

                    try:
                        # Try multiple username selectors (different Wells Fargo pages)
                        username_input = None
                        for selector in ["input[id=j_username]", "input[name=userid]", "input[type=text]"]:
                            try:
                                username_input = await page.select(selector, timeout=2)
                                if username_input:
                                    print(f"Found username input with: {selector}")
                                    break
                            except:
                                continue

                        if username_input:
                            await username_input.click()
                            await username_input.clear_input()
                            await username_input.send_keys(username)
                            print("✓ Username entered")
                        else:
                            print("⚠ Could not find username field")

                        # Try multiple password selectors
                        password_input = None
                        for selector in ["input[id=j_password]", "input[name=password]", "input[type=password]"]:
                            try:
                                password_input = await page.select(selector, timeout=2)
                                if password_input:
                                    print(f"Found password input with: {selector}")
                                    break
                            except:
                                continue

                        if password_input:
                            await password_input.clear_input()
                            await password_input.send_keys(password)
                            print("✓ Password entered")
                        else:
                            print("⚠ Could not find password field")

                        # Try multiple sign-on button selectors
                        sign_on_button = None
                        for selector in [".button.button--login.button--signOn", "button[type=submit]", "button[name=btnSignon]"]:
                            try:
                                sign_on_button = await page.select(selector, timeout=2)
                                if sign_on_button:
                                    print(f"Found sign-on button with: {selector}")
                                    break
                            except:
                                continue

                        if sign_on_button:
                            await sign_on_button.click()
                            print("✓ Sign-on button clicked")
                            print("Waiting for authentication to complete...")
                            await asyncio.sleep(5)
                        else:
                            print("⚠ Could not find sign-on button")
                            print("Please manually click the sign-on button in the browser")
                            input("Press ENTER when login is complete...")

                    except Exception as e:
                        print(f"Error re-entering credentials: {e}")
                        traceback.print_exc()
                        print("\nPlease manually complete the login in the browser")
                        input("Press ENTER when login is complete...")
                else:
                    print("Not on login page - checking if authentication succeeded...")
                    await asyncio.sleep(2)

        # Handle 2FA if needed (check for INTERDICTION in URL as per reference)
        try:
            current_url = await page.evaluate("window.location.href")
        except:
            current_url = page.url

        if "dest=INTERDICTION" in current_url:
            print("Wells Fargo 2FA required.")

            # Select mobile option (as per reference)
            try:
                # Find all list items with role="listitem"
                list_items = await page.select_all('[role="listitem"]')
                for item in list_items:
                    text = await item.text
                    if "Mobile" in text or "mobile" in text:
                        # Find button within this list item and click it
                        button = await item.select("button")
                        if button:
                            await button.click()
                            print("Selected mobile 2FA option")
                            break

                await asyncio.sleep(5)  # Wait for OTP page to load
            except Exception as e:
                print(f"Error selecting mobile option: {e}")

            # Enter OTP code
            try:
                otp_code = input("Enter Wells Fargo OTP code: ")

                # Use exact selector from reference
                otp_input = await page.select("#otp")
                await otp_input.send_keys(otp_code)

                # Click submit button (exact selector from reference)
                submit_button = await page.select('button[type="submit"]')
                await submit_button.click()

                await asyncio.sleep(5)  # Wait for 2FA to complete
                print("2FA code submitted")
            except Exception as e:
                print(f"Error during 2FA: {e}")

        # Verify login was successful
        try:
            current_url = await page.evaluate("window.location.href")
            page_title = await page.evaluate("document.title")
        except:
            current_url = page.url
            page_title = ""

        # Check if we're already on the overview/accounts page
        already_on_overview = any(keyword in current_url.lower() for keyword in ["brokoverview", "wellstrade"]) or \
                             any(keyword in page_title.lower() for keyword in ["brokerage overview", "accounts"])

        if "login" in current_url.lower() and not already_on_overview:
            print("✗ Wells Fargo login failed - still on login page")
            print(f"Current URL: {current_url}")
            return None

        # Navigate to accounts overview page to enable account discovery
        # This ensures we have the accounts table available
        print("Login successful! Loading account information...")

        if not already_on_overview:
            try:
                await page.get("https://wfawellstrade.wellsfargo.com/BW/brokoverview.do")
                await asyncio.sleep(3)
            except Exception as e:
                print(f"Warning: Could not navigate to overview page: {e}")
        else:
            print("Already on accounts overview page")

        print("✓ Wells Fargo authenticated successfully")
        return browser

    except Exception as e:
        print(f"Error during Wells Fargo authentication: {e}")
        traceback.print_exc()
        if browser:
            await browser.stop()
        return None


async def _wellsfargo_get_browser(session_info):
    """Get Wells Fargo browser session, authenticating if necessary."""
    # For now, always authenticate to get fresh session
    # In production, you'd cache the browser instance
    browser = await _wellsfargo_authenticate(session_info)
    return browser


async def _extract_dynamic_x_param(page):
    """Extract the dynamic _x parameter from current URL using regex (as per reference)."""
    try:
        # Use JavaScript to get real URL since page.url may be stuck at about:blank
        try:
            current_url = await page.evaluate("window.location.href")
        except:
            current_url = page.url

        match = re.search(r'_x=([^&]+)', current_url)
        if match:
            return f"_x={match.group(1)}"
        return ""
    except Exception as e:
        print(f"Error extracting x_param: {e}")
        return ""


async def _discover_accounts(browser):
    """
    Discover all Wells Fargo accounts by parsing the accounts page.
    Returns list of dicts with account info: {index, name, number, balance, x_param}
    """
    try:
        page = browser.main_tab

        # Ensure we're on the accounts overview page; if not, navigate there and wait
        try:
            current_url = await page.evaluate("window.location.href")
        except Exception:
            current_url = page.url

        if "brokoverview" not in (current_url or "").lower():
            try:
                await page.get("https://wfawellstrade.wellsfargo.com/BW/brokoverview.t.do")
                await asyncio.sleep(3)
            except Exception as nav_err:
                print(f"Warning: Unable to navigate to Wells Fargo accounts overview: {nav_err}")
        else:
            # Give the overview table a moment to populate after login
            await asyncio.sleep(2)

        # Poll for the accounts table to populate before parsing to capture all accounts
        soup = BeautifulSoup("", 'html.parser')
        account_rows = []
        for attempt in range(6):
            try:
                row_count = await page.evaluate("document.querySelectorAll('tr[data-p_account]').length")
            except Exception:
                row_count = 0

            if row_count > 0:
                html = await page.get_content()
                soup = BeautifulSoup(html, 'html.parser')
                account_rows = soup.select('tr[data-p_account]')
                if account_rows:
                    break

            await asyncio.sleep(2)

        # Extract x parameter from URL
        x_param = await _extract_dynamic_x_param(page)
        print(f"Extracted x_param: {x_param[:50] if x_param else 'None'}...")

        # If still no rows found, fall back to parsing whatever content is available
        if not account_rows:
            html = await page.get_content()
            soup = BeautifulSoup(html, 'html.parser')
            account_rows = soup.select('tr[data-p_account]')

        print(f"Found {len(account_rows)} account rows in HTML")

        # Debug: also check for any table rows
        all_table_rows = soup.select('table tr')
        print(f"Total table rows on page: {len(all_table_rows)}")

        accounts = []
        account_index = 0  # Separate counter for non-"-1" accounts
        for idx, row in enumerate(account_rows):
            # Skip "All Accounts" row (data-p_account="-1")
            account_attr = row.get('data-p_account')
            if account_attr == '-1':
                print(f"Skipping 'All Accounts' row (data-p_account={account_attr})")
                continue

            try:
                # Get account name from rowheader
                name_elem = row.select_one('[role="rowheader"] .ellipsis')
                account_name = name_elem.get_text(strip=True) if name_elem else f"Account {account_index}"

                # Get account number
                number_elem = row.select_one('div:not(.ellipsis-container)')
                account_number = ""
                if number_elem:
                    account_number = number_elem.get_text(strip=True).replace('*', '')

                # Get balance from last td with data-sort-value
                balance_cells = row.select('td[data-sort-value]')
                balance = 0.0
                if balance_cells:
                    balance_text = balance_cells[-1].get_text(strip=True)
                    # Remove currency symbols and commas
                    balance_text = balance_text.replace('$', '').replace(',', '')
                    try:
                        balance = float(balance_text)
                    except (ValueError, TypeError):
                        pass

                accounts.append({
                    'index': account_index,  # Use separate counter, not enumerate idx
                    'data_p_account': account_attr,  # Store the actual attribute value too
                    'name': account_name,
                    'number': account_number,
                    'balance': balance,
                    'x_param': x_param
                })

                print(f"Found account #{account_index}: {account_name} ({account_number}) - ${balance:,.2f} [data-p_account={account_attr}]")
                account_index += 1

            except Exception as e:
                print(f"Error parsing account row {idx}: {e}")
                continue

        if not accounts:
            print("Warning: No accounts found on page")
            # Fallback: return single account with index 0
            accounts = [{
                'index': 0,
                'name': 'Default Account',
                'number': '',
                'balance': 0.0,
                'x_param': x_param
            }]

        return accounts

    except Exception as e:
        print(f"Error discovering accounts: {e}")
        traceback.print_exc()
        # Fallback to single account
        x_param = await _extract_dynamic_x_param(page) if page else ""
        return [{
            'index': 0,
            'name': 'Default Account',
            'number': '',
            'balance': 0.0,
            'x_param': x_param
        }]


async def _wellsfargo_parse_holdings_table(html):
    """Parse Wells Fargo holdings table HTML using exact selectors from reference."""
    soup = BeautifulSoup(html, 'html.parser')
    holdings = []

    # Find holdings rows using exact selector from reference: tbody > tr.level1
    rows = soup.select('tbody > tr.level1')

    for row in rows:
        try:
            # Symbol: a.navlink.quickquote (remove ",popup" suffix as per reference)
            symbol_elem = row.select_one('a.navlink.quickquote')
            if not symbol_elem:
                continue

            symbol_text = symbol_elem.get_text(strip=True)
            symbol = symbol_text.split(',')[0].strip()

            if not symbol or symbol.lower() == 'popup':
                # Skip empty helper rows
                continue

            # Name: td[role="rowheader"] .data-content > div:last-child
            name_elem = row.select_one('td[role="rowheader"] .data-content > div:last-child')
            name = name_elem.get_text(strip=True) if name_elem else "N/A"

            # Quantity and Price: td.datanumeric cells
            data_cells = row.select('td.datanumeric')
            if len(data_cells) < 3:
                continue

            # Quantity is index [1], price is index [2] as per reference
            quantity_cell = data_cells[1]
            quantity_div = quantity_cell.select_one('div:first-child')
            quantity_text = quantity_div.get_text(strip=True) if quantity_div else "0"
            quantity = float(quantity_text.replace(',', ''))

            price_cell = data_cells[2]
            price_div = price_cell.select_one('div:first-child')
            price_text = price_div.get_text(strip=True) if price_div else "0"
            price = float(price_text.replace('$', '').replace(',', ''))

            # Only add if quantity > 0 (as per reference)
            if quantity > 0:
                holdings.append({
                    "symbol": symbol,
                    "name": name,
                    "quantity": quantity,
                    "price": price,
                    "value": quantity * price
                })

        except (ValueError, IndexError, AttributeError) as e:
            print(f"Error parsing holdings row: {e}")
            continue

    return holdings


async def wellsfargoGetHoldings(ticker=None):
    """Get holdings from all Wells Fargo accounts."""
    from .session_manager import session_manager
    session = await session_manager.get_session("WellsFargo")
    if not session:
        print("No Wells Fargo credentials supplied, skipping")
        return None

    browser = None
    try:
        # Get authenticated browser
        browser = await _wellsfargo_get_browser(session)
        if not browser:
            print("Failed to authenticate with Wells Fargo")
            return None

        page = browser.main_tab

        # Discover all accounts
        print("Discovering Wells Fargo accounts...")
        accounts = await _discover_accounts(browser)
        print(f"Found {len(accounts)} Wells Fargo account(s)")

        all_holdings = {}

        # Iterate through each account
        for account in accounts:
            account_name = account['name']
            account_number = account['number']
            account_index = account['index']
            x_param = account['x_param']

            print(f"\nFetching holdings for: {account_name}")

            try:
                # Navigate to holdings page for this account
                holdings_url = f"https://wfawellstrade.wellsfargo.com/BW/holdings.do?account={account_index}"
                if x_param:
                    holdings_url += f"&{x_param}"

                print(f"  Navigating to: {holdings_url[:120]}...")
                await page.get(holdings_url)
                await asyncio.sleep(3)

                # Check if we got an error page
                page_content = await page.get_content()
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
                holdings = await _wellsfargo_parse_holdings_table(html)

                # Filter by ticker if specified
                if ticker:
                    holdings = [h for h in holdings if h["symbol"].upper() == ticker.upper()]

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

    except Exception as e:
        print(f"Error getting Wells Fargo holdings: {e}")
        traceback.print_exc()
        return None
    finally:
        if browser:
            await browser.stop()


async def wellsfargoTrade(side, qty, ticker, price):
    """Execute a trade on Wells Fargo Advisors."""
    from .session_manager import session_manager
    session = await session_manager.get_session("WellsFargo")
    if not session:
        print("No Wells Fargo credentials supplied, skipping")
        return None

    browser = None
    try:
        # Get authenticated browser
        browser = await _wellsfargo_get_browser(session)
        if not browser:
            print("Failed to authenticate with Wells Fargo")
            return None

        page = browser.main_tab

        # Discover all accounts
        print("Discovering Wells Fargo accounts...")
        accounts = await _discover_accounts(browser)
        print(f"Found {len(accounts)} Wells Fargo account(s)")

        success_count = 0

        # Trade on all accounts
        for account in accounts:
            account_name = account['name']
            account_index = account['index']
            x_param = account['x_param']

            print(f"\nTrading on: {account_name}")

            try:
                # Navigate to trade page for this account
                trade_url = f"https://wfawellstrade.wellsfargo.com/BW/equity.do?account={account_index}&symbol=&selectedAction="
                if x_param:
                    trade_url += f"&{x_param}"

                await page.get(trade_url)
                await asyncio.sleep(2)

                # Select Buy or Sell action
                action_select = await page.select("select[id=action]")
                if action_select:
                    action_value = "BUY" if side == "buy" else "SELL"
                    await action_select.select_option(action_value)

                # Enter symbol
                symbol_input = await page.select("input[id=symbol]")
                if symbol_input:
                    await symbol_input.clear_input()
                    await symbol_input.send_keys(ticker)
                    await asyncio.sleep(1)

                    # Wait for quote to load
                    await asyncio.sleep(2)

                # Enter quantity
                quantity_input = await page.select("input[id=quantity]")
                if quantity_input:
                    await quantity_input.clear_input()
                    await quantity_input.send_keys(str(qty))

                # Determine order type based on price
                if price and price >= 2.00:
                    # Use limit order
                    order_type_select = await page.select("select[id=orderType]")
                    if order_type_select:
                        await order_type_select.select_option("LIMIT")

                    # Enter limit price
                    limit_price_input = await page.select("input[id=limitPrice]")
                    if limit_price_input:
                        await limit_price_input.clear_input()
                        # Adjust price by $0.01
                        if side == "buy":
                            adjusted_price = round(price + 0.01, 2)
                        else:
                            adjusted_price = round(price - 0.01, 2)
                        await limit_price_input.send_keys(str(adjusted_price))
                else:
                    # Use market order for stocks under $2
                    order_type_select = await page.select("select[id=orderType]")
                    if order_type_select:
                        await order_type_select.select_option("MARKET")

                # Set time in force to Day
                time_select = await page.select("select[id=timeInForce]")
                if time_select:
                    await time_select.select_option("DAY")

                # Click preview button
                preview_button = await page.select("button[id=previewBtn]")
                if preview_button:
                    await preview_button.click()
                    await asyncio.sleep(2)

                # Check for errors on preview page
                error_msg = await page.select(".error-message")
                if error_msg:
                    error_text = await error_msg.text
                    print(f"Wells Fargo order error for {account_name}: {error_text}")
                    continue

                # Check for confirmation button (indicates order is valid)
                confirm_button = await page.select("button[id=confirmBtn]")
                if not confirm_button:
                    print(f"Wells Fargo order cannot be placed on {account_name} - no confirmation button found")
                    continue

                # Check if we should actually submit (not dry-run)
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    print(f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}")
                    success_count += 1
                    continue

                # Submit the order
                await confirm_button.click()
                await asyncio.sleep(2)

                # Check for success confirmation
                success_msg = await page.select(".success-message")
                if success_msg:
                    action_str = "Bought" if side == "buy" else "Sold"
                    print(f"{action_str} {qty} shares of {ticker} on {account_name}")
                    success_count += 1
                else:
                    print(f"Wells Fargo order may have failed on {account_name} - no success message found")

            except Exception as e:
                print(f"Error trading on {account_name}: {e}")
                traceback.print_exc()
                continue

        # Return True if at least one account succeeded
        return success_count > 0 if success_count else None

    except Exception as e:
        print(f"Error during Wells Fargo trade: {e}")
        traceback.print_exc()
        return None
    finally:
        if browser:
            await browser.stop()


async def get_wellsfargo_session(session_manager):
    """Get or create Wells Fargo session."""
    if "wellsfargo" not in session_manager._initialized:
        WELLSFARGO_USER = os.getenv("WELLSFARGO_USER")
        WELLSFARGO_PASS = os.getenv("WELLSFARGO_PASS")
        WELLSFARGO_PHONE = os.getenv("WELLSFARGO_PHONE_SUFFIX")  # Optional phone suffix for 2FA

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
