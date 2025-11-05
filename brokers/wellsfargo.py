"""Wells Fargo Advisors broker integration using zendriver."""

import os
import re
import asyncio
import traceback
from bs4 import BeautifulSoup
from zendriver import Browser
from .base import rate_limiter


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
    await rate_limiter.wait_if_needed("WellsFargo")

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
    await rate_limiter.wait_if_needed("WellsFargo")

    from .session_manager import session_manager
    session = await session_manager.get_session("WellsFargo")
    if not session:
        print("No Wells Fargo credentials supplied, skipping")
        return None

    # Save original price parameter (user-specified price, if any)
    user_specified_price = price

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
            # Use data_p_account if available, otherwise fall back to index
            account_param = account.get('data_p_account', account['index'])
            x_param = account['x_param']

            # Reset price for this account (use per-account copy to avoid stale pricing)
            # Each account needs to fetch its own quote independently
            price_for_account = user_specified_price

            print(f"\nTrading on: {account_name} (account param: {account_param})")

            try:
                # Navigate to trade page for this account WITH symbol pre-filled to get quote
                action_value = "BUY" if side == "buy" else "SELL"
                trade_url = f"https://wfawellstrade.wellsfargo.com/BW/equity.do?account={account_param}&symbol={ticker}&selectedAction={action_value}"
                if x_param:
                    trade_url += f"&{x_param}"

                print(f"Navigating to trade URL: {trade_url}")
                await page.get(trade_url)

                # Wait for page to load completely
                await asyncio.sleep(3)

                # Debug: Check if we're on the right page
                try:
                    current_url = await page.evaluate("window.location.href")
                except Exception:
                    current_url = page.url
                print(f"Current URL after navigation: {current_url}")

                # Wait for the page body to be fully rendered
                await page.wait_for("body", timeout=10)

                # Set buy/sell action by directly manipulating form
                print("Setting buy/sell action...")
                try:
                    action_text = "Buy" if side == "buy" else "Sell"
                    # Set the hidden field and update button text
                    result = await page.evaluate(f"""
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

                # Verify quote loaded (symbol pre-filled via URL)
                print("Checking if quote loaded...")
                try:
                    # Wait a moment for quote to fully load
                    await asyncio.sleep(2)

                    # Verify quote loaded by checking if last price is populated
                    quote_loaded = await page.evaluate("document.getElementById('last')?.value")
                    if quote_loaded and quote_loaded != 'None' and quote_loaded.strip():
                        print(f"Quote loaded: ${quote_loaded}")
                    else:
                        print(f"Warning: Quote may not have loaded (value: '{quote_loaded}'), will use default pricing")
                except Exception as e:
                    print(f"Error checking quote: {e}")

                # Enter quantity
                print("Setting quantity...")
                try:
                    result = await page.evaluate(f"""
                        (function() {{
                            const qtyInput = document.getElementById('OrderQuantity');
                            if (qtyInput) {{
                                qtyInput.value = '{qty}';
                                // Trigger events
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
                        # Try to get last price from the form
                        last_price_str = await page.evaluate("document.getElementById('last')?.value")
                        print(f"Retrieved last price: {last_price_str}")

                        if last_price_str and last_price_str.strip():
                            price_for_account = float(last_price_str.strip())
                            print(f"Current stock price: ${price_for_account}")
                        else:
                            # Default to $1 if we can't get price (will trigger limit order)
                            print("Could not get price from page, defaulting to $1 (will use limit order)")
                            price_for_account = 1.00
                    except Exception as e:
                        print(f"Error getting price: {e}")
                        price_for_account = 1.00

                # Determine order type based on price
                # Wells Fargo requires limit orders for low-priced stocks (under $2)
                # Also use limit order if user explicitly specified a price
                use_limit_order = False
                limit_reason = ""

                if user_specified_price:
                    # User explicitly specified a price - use limit order
                    use_limit_order = True
                    limit_reason = "user specified price"
                elif price_for_account and price_for_account < 2.00:
                    # Low-priced stock - Wells Fargo requires limit order
                    use_limit_order = True
                    limit_reason = "low-priced stock (Wells Fargo requirement)"

                if use_limit_order:
                    # Use limit order
                    print(f"Setting order type to LIMIT ({limit_reason})...")
                    try:
                        result = await page.evaluate("""
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

                    # Enter limit price
                    await asyncio.sleep(0.5)

                    # If user specified a price explicitly, use that
                    if user_specified_price:
                        adjusted_price = round(user_specified_price, 2)
                        print(f"Using user-specified price ${adjusted_price}")
                    else:
                        # Calculate limit price based on last price:
                        # - For buys: last price + $0.01
                        # - For sells: last price - $0.01
                        if side == "buy":
                            adjusted_price = round(price_for_account + 0.01, 2)
                            print(f"Using last price ${price_for_account} + $0.01 for buy")
                        else:  # sell
                            adjusted_price = round(price_for_account - 0.01, 2)
                            print(f"Using last price ${price_for_account} - $0.01 for sell")

                    # Ensure price doesn't go negative
                    if adjusted_price <= 0:
                        adjusted_price = 0.01

                    try:
                        result = await page.evaluate(f"""
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
                    # Use market order
                    print("Setting order type to MARKET...")
                    try:
                        result = await page.evaluate("""
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
                    result = await page.evaluate("""
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

                # Wait a moment for form to be fully populated
                await asyncio.sleep(1)

                # Click continue/preview button
                print("Clicking continue button...")
                continue_button = await page.select("button[id=actionbtnContinue]", timeout=5)
                if continue_button:
                    await continue_button.click()
                    await asyncio.sleep(3)
                else:
                    print("Warning: Could not find continue button")
                    continue

                # Check what page we're on now
                print("Checking order preview page...")
                try:
                    page_title = await page.evaluate("document.title")
                    current_url = await page.evaluate("window.location.href")
                    print(f"Preview page title: {page_title}")
                    print(f"Preview URL: {current_url}")
                except Exception:
                    pass

                # Check for errors on preview page (ignore warnings)
                try:
                    # Get only actual ERROR messages in the text, not warnings
                    error_texts = await page.evaluate("""
                        Array.from(document.querySelectorAll('body')).map(el => el.textContent).join('\\n')
                    """)

                    # Check if there are actual "Error:" messages (not "Warning:")
                    has_errors = False
                    error_messages = []
                    if error_texts:
                        lines = error_texts.split('\\n')
                        for line in lines:
                            line = line.strip()
                            if line.startswith('Error:') and 'Warning:' not in line:
                                has_errors = True
                                error_messages.append(line[:200])  # Limit length
                                if len(error_messages) >= 3:
                                    break

                    if has_errors:
                        print(f"Wells Fargo order errors for {account_name}:")
                        for err in error_messages:
                            print(f"  - {err}")
                        continue
                    else:
                        # No errors, but there might be warnings - that's OK
                        print("Order preview looks good (warnings are OK)")
                except Exception as ex:
                    print(f"Error checking for validation errors: {ex}")

                # Look for a confirmation/submit button on preview page
                # According to reference, the submit button has class .btn-wfa-primary.btn-wfa-submit
                confirm_button = None
                try:
                    confirm_button = await page.select(".btn-wfa-primary.btn-wfa-submit", timeout=5)
                    if confirm_button:
                        print("Found submit button (.btn-wfa-primary.btn-wfa-submit)")
                except Exception:
                    # Fallback to button ID
                    for button_id in ['actionbtnContinue', 'confirmBtn', 'submitBtn']:
                        try:
                            confirm_button = await page.select(f"button[id={button_id}]", timeout=2)
                            if confirm_button:
                                print(f"Found confirm button: {button_id}")
                                break
                        except Exception:
                            continue

                if not confirm_button:
                    print(f"Wells Fargo order cannot be placed on {account_name} - no confirmation button found")
                    # Debug: show buttons on page
                    try:
                        buttons = await page.evaluate("""
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

                # Check if we should actually submit (not dry-run)
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    print(f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}")
                    success_count += 1
                    continue

                # Submit the order
                print("Submitting order...")
                await confirm_button.click()
                await asyncio.sleep(3)

                # Check for success confirmation
                try:
                    final_title = await page.evaluate("document.title")
                    print(f"After submission - Title: {final_title}")

                    # Look for success indicators (multiple patterns)
                    page_text = await page.evaluate("document.body.textContent")

                    # Check for various success patterns
                    success_patterns = [
                        'order has been placed',
                        'successfully',
                        'Success',
                        'confirmed',
                        'Confirmed',
                        'Order Number',
                        'order number',
                        'has been received',
                        'Acknowledgment'
                    ]

                    is_success = any(pattern.lower() in page_text.lower() for pattern in success_patterns)

                    # Also check URL - if it changed to confirmation page
                    final_url = await page.evaluate("window.location.href")
                    if 'confirmation' in final_url.lower() or 'orderack' in final_url.lower():
                        is_success = True

                    if is_success:
                        action_str = "Bought" if side == "buy" else "Sold"
                        print(f"✓ {action_str} {qty} shares of {ticker} on {account_name}")
                        success_count += 1
                    else:
                        print(f"Wells Fargo order may have failed on {account_name} - no clear success confirmation")
                        print(f"Final URL: {final_url}")

                        # Check for errors on the page
                        errors_on_page = await page.evaluate("""
                            Array.from(document.querySelectorAll('.error, .error-message, [class*="error"], [class*="Error"]'))
                            .map(el => el.textContent.trim())
                            .filter(text => text.length > 0 && text.includes('Error'))
                            .slice(0, 2)
                        """)
                        if errors_on_page:
                            for error in errors_on_page:
                                # Clean up error message
                                clean_error = ' '.join(error.split())
                                print(f"  ⚠ {clean_error}")
                except Exception as e:
                    print(f"Could not verify order success: {e}")

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
