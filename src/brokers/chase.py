"""Chase Invest broker integration using zendriver."""

import os
import json
import asyncio
import logging
import traceback
from brokers.browser_utils import create_browser, stop_browser, get_page_url, wait_for_ready_state, poll_for_condition
from brokers.base import broker_event

# Chase API endpoints
POSITIONS_URL = "https://secure.chase.com/svc/wr/dwm/secure/gateway/investments/servicing/inquiry-maintenance/digital-investment-positions/v2/positions"
ACCOUNT_OPTIONS_URL = "https://secure.chase.com/svc/rr/accounts/secure/v2/portfolio/account/options/list2"
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
            await stop_browser(self._browser, log=logger)
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
            self._browser = await create_browser(
                headless=self._headless, user_agent=None
            )

            # Navigate directly to Chase login page
            self._page = await self._browser.get(
                "https://secure.chase.com/web/auth/?fromOrigin=https://secure.chase.com"
            )
            await wait_for_ready_state(self._page)
            broker_event("Chase login page loaded", logger=logger)

            try:
                await self._page.wait_for(
                    selector="input[type=password]", timeout=20
                )
            except asyncio.TimeoutError:
                broker_event("Login form not found after 20s", level="warning", logger=logger)

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
                    username_input = await self._page.select(selector, timeout=5)
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
                js_result = await self._page.evaluate(js_script)
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
                        password_input = await self._page.select(selector, timeout=5)
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
                        signin_button = await self._page.select(selector, timeout=5)
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
                        signin_button = await self._page.find("Sign in", best_match=True)
                    except (
                        asyncio.TimeoutError,
                        AttributeError,
                        RuntimeError,
                        TypeError,
                    ):
                        signin_button = None
                if not signin_button:
                    try:
                        signin_button = await self._page.find("Log on", best_match=True)
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
            current_url = await get_page_url(self._page)
            page_text = await self._page.evaluate("document.body?.innerText || ''")

            broker_event(f"Post-login URL: {current_url}", logger=logger)
            broker_event(
                f"Post-login page preview: {page_text[:300]}...", logger=logger
            )

            # Check for 2FA - look at both URL and page content
            url_lower = current_url.lower()
            text_lower = page_text.lower()
            needs_verification = (
                "verification" in url_lower
                or "mfa" in url_lower
                or "authenticate" in url_lower
                or "identify" in url_lower
                or "recognizeuser" in url_lower
                or "verify" in text_lower
                or ("send" in text_lower and "code" in text_lower)
                or ("confirm" in text_lower and "phone" in text_lower)
                or "push notification" in text_lower
                or "check your phone" in text_lower
            )

            if needs_verification:
                # Auto-select "Send a notification to my phone" and click Next
                clicked_notify = await self._page.evaluate("""
                    (() => {
                        // Strategy 1: Click the radio input directly
                        const radios = document.querySelectorAll('input[type="radio"]');
                        for (const radio of radios) {
                            const label = radio.closest('label') || radio.parentElement;
                            const text = (label?.textContent || '').toLowerCase();
                            if (text.includes('send a notification')) {
                                radio.click();
                                return {method: 'radio', tag: radio.tagName};
                            }
                        }
                        // Strategy 2: Click the label element
                        const labels = document.querySelectorAll('label');
                        for (const label of labels) {
                            if (label.textContent?.toLowerCase().includes('send a notification')) {
                                label.click();
                                return {method: 'label', tag: label.tagName};
                            }
                        }
                        // Strategy 3: Click any element with that text (narrowest match)
                        const all = document.querySelectorAll('span, div, a, button');
                        let best = null;
                        for (const el of all) {
                            const text = (el.textContent || '').trim().toLowerCase();
                            if (text.includes('send a notification')) {
                                if (!best || el.textContent.length < best.textContent.length) {
                                    best = el;
                                }
                            }
                        }
                        if (best) {
                            best.click();
                            return {method: 'text', tag: best.tagName};
                        }
                        return null;
                    })()
                """)

                if clicked_notify:
                    broker_event(
                        f"Selected 'Send a notification to my phone' via {clicked_notify.get('method')}",
                        logger=logger,
                    )
                    await asyncio.sleep(2)

                    # Click the "Next" button to confirm
                    clicked_next = await self._page.evaluate("""
                        (() => {
                            const btns = Array.from(document.querySelectorAll('button'));
                            const next = btns.find(b => {
                                const text = b.textContent.trim().toLowerCase();
                                return text === 'next';
                            });
                            if (next && !next.disabled) { next.click(); return true; }
                            return false;
                        })()
                    """)
                    if clicked_next:
                        broker_event("Clicked 'Next' to send push notification", logger=logger)
                    else:
                        broker_event("'Next' button not found or disabled", level="warning", logger=logger)

                    await asyncio.sleep(3)

                    # Log page state after clicking Next for debugging
                    post_next_text = await self._page.evaluate("document.body?.innerText || ''")
                    post_next_url = await get_page_url(self._page)
                    logger.debug("Post-Next URL: %s", post_next_url)
                    logger.debug("Post-Next page text: %s", post_next_text[:500])

                    # Check if notification was actually sent (step 2 page)
                    step2_lower = post_next_text.lower()
                    if "we sent" in step2_lower or "check your" in step2_lower or "approve" in step2_lower:
                        broker_event("Push notification sent, waiting for approval...", logger=logger)
                    elif "send a notification" in step2_lower:
                        # Still on step 1 — selection didn't take effect
                        broker_event(
                            "Still on verification step 1 — radio selection may not have worked",
                            level="warning",
                            logger=logger,
                        )
                else:
                    broker_event(
                        "Could not find 'Send a notification' option",
                        level="warning",
                        logger=logger,
                    )

                broker_event(
                    "Waiting for verification to complete (up to 2 minutes)...",
                    level="warning",
                    logger=logger,
                )

                # Poll: look for the page to leave the verification flow entirely.
                # The URL may keep "recognizeuser" during step 2, so primarily check
                # for the dashboard/overview page or absence of verification page text.
                async def _check_verification_done():
                    url = (await get_page_url(self._page)).lower()
                    # If URL has moved to dashboard/overview, we're done
                    if "dashboard" in url or "overview" in url:
                        return True
                    # If URL no longer has auth/verification markers, we're done
                    if "recognizeuser" not in url and "authenticate" not in url and "verification" not in url:
                        return True
                    # Still on verification page — check page text for step 2 completion
                    text = (await self._page.evaluate("document.body?.innerText || ''") or "").lower()
                    # Step 2 text means still waiting
                    if "we sent" in text or "check your" in text or "push notification" in text:
                        return False
                    # Step 1 text means still stuck
                    if "send a notification" in text or "how should we" in text:
                        return False
                    # No verification text found — likely moved past it
                    return True

                if await poll_for_condition(_check_verification_done, timeout=120, interval=2):
                    broker_event("✓ Verification completed successfully", logger=logger)

            # Wait for post-login processing page to complete
            async def _check_processing_done():
                url = (await get_page_url(self._page)).lower()
                return "processstatus" not in url

            await poll_for_condition(_check_processing_done, timeout=60, interval=2)

            # Navigate to dashboard overview (shows investment account summaries)
            broker_event("Navigating to Chase dashboard...", logger=logger)
            await self._page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/overview"
            )

            async def _check_dashboard_loaded():
                text = await self._page.evaluate("document.body?.innerText || ''")
                return text and len(text.strip()) > 200

            await poll_for_condition(_check_dashboard_loaded, timeout=15, interval=1)

            # Verify login success
            current_url = await get_page_url(self._page)

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
                await stop_browser(self._browser)
                self._browser = None
            self._is_authenticated = False
            raise

    async def _call_chase_api(self, url, method="POST", content_type="application/json", body=None):
        """Execute a fetch() call in the browser context.

        Uses a window-stash pattern because zendriver's evaluate() does not
        properly await async IIFEs (returns {} instead of the resolved value).
        We fire the fetch, store the result on window, then read it back.

        Returns the parsed JSON response, or None on failure.
        """
        body_js = json.dumps(body) if body else "undefined"
        stash_key = f"__chaseApi_{id(self)}"

        # Fire the fetch and stash result on window
        fire_js = f"""
            (function() {{
                window['{stash_key}'] = null;
                fetch('{url}', {{
                    method: '{method}',
                    credentials: 'include',
                    headers: {{
                        'Accept': 'application/json, text/plain, */*',
                        'Content-Type': '{content_type}',
                        'x-jpmc-csrf-token': 'NONE'
                    }},
                    body: {body_js}
                }}).then(function(response) {{
                    return response.text().then(function(text) {{
                        if (response.ok) {{
                            try {{
                                window['{stash_key}'] = JSON.parse(text);
                            }} catch (e) {{
                                window['{stash_key}'] = {{ error: 'parse', text: text.substring(0, 1000) }};
                            }}
                        }} else {{
                            window['{stash_key}'] = {{ error: response.status, text: text.substring(0, 500) }};
                        }}
                    }});
                }}).catch(function(e) {{
                    window['{stash_key}'] = {{ error: e.message }};
                }});
                return 'started';
            }})()
        """

        await self._page.evaluate(fire_js)

        # Poll for the result
        async def _check_result():
            result = await self._page.evaluate(f"window['{stash_key}']")
            return result is not None

        if not await poll_for_condition(_check_result, timeout=10, interval=0.5):
            logger.debug("API call to %s timed out waiting for response", url)
            return None

        result = await self._page.evaluate(f"window['{stash_key}']")

        # Clean up
        await self._page.evaluate(f"delete window['{stash_key}']")

        return result

    async def _get_account_ids(self):
        """Get all investment account IDs using the correct POST API."""
        self._accounts = None

        try:
            broker_event("Fetching account IDs via API...", logger=logger)

            # Navigate to investments page to establish proper context for API calls
            await self._page.get(
                "https://secure.chase.com/web/auth/dashboard#/dashboard/investments"
            )
            await asyncio.sleep(3)

            api_response = await self._call_chase_api(
                ACCOUNT_OPTIONS_URL,
                content_type="application/x-www-form-urlencoded",
                body="filterOption=ALL",
            )

            logger.debug("Account options API response: %s", str(api_response)[:500])

            if not api_response or "error" in api_response:
                error_msg = api_response.get("error", "Unknown") if api_response else "No response"
                broker_event(
                    f"Account options API failed: {error_msg}",
                    level="warning",
                    logger=logger,
                )
                return []

            accounts = api_response.get("accounts", [])
            account_metadata = []
            account_ids = []

            for acct in accounts:
                account_id = acct.get("accountId")
                if not account_id:
                    continue
                account_ids.append(account_id)
                account_metadata.append({
                    "id": account_id,
                    "name": acct.get("nickname") or acct.get("mask") or account_id,
                    "mask": acct.get("mask") or "",
                    "retirement": acct.get("retirement", False),
                })

            if account_metadata:
                self._accounts = account_metadata

            broker_event(f"Found {len(account_ids)} account(s)", logger=logger)
            for acct in account_metadata:
                broker_event(f"  - {acct['name']} ({acct['id']})", logger=logger)

            return account_ids

        except Exception as e:
            broker_event(
                f"Error getting account IDs: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()
            return []

    async def _fetch_holdings_json(self, account_id):
        """Fetch holdings for a specific account using the correct POST API."""
        holdings = []

        try:
            request_body = json.dumps({
                "selectorIdentifier": account_id,
                "selectorCode": "ACCOUNT",
                "intradayUpdateIndicator": True,
                "topPositionsRequestedRecordCount": 50,
                "accountStatusIndicator": True,
                "moversRequestedRecordCount": 5,
            })

            api_response = await self._call_chase_api(
                POSITIONS_URL,
                content_type="application/json",
                body=request_body,
            )

            logger.debug("Positions API response for %s: %s", account_id, str(api_response)[:500])

            if not api_response or "error" in api_response:
                error_msg = api_response.get("error", "Unknown") if api_response else "No response"
                broker_event(
                    f"Positions API failed for {account_id}: {error_msg}",
                    level="warning",
                    logger=logger,
                )
                return holdings

            # Parse equity positions
            for pos in api_response.get("positions", []):
                try:
                    sec_detail = pos.get("securityIdDetail", {})
                    symbol = sec_detail.get("symbolSecurityIdentifier") or sec_detail.get("snapQuoteOptionSymbolCode", "")
                    if not symbol:
                        continue

                    quantity = float(pos.get("tradedUnitQuantity", 0))
                    if quantity <= 0:
                        continue

                    market_value = float(pos.get("marketValue", {}).get("baseValueAmount", 0))
                    price = float(pos.get("marketPrice", {}).get("baseValueAmount", 0))
                    cost_basis = float(pos.get("tradedCost", {}).get("baseValueAmount", 0))
                    unrealized_gl = float(pos.get("unrealizedGainLoss", {}).get("baseValueAmount", 0))

                    holdings.append({
                        "symbol": symbol,
                        "quantity": quantity,
                        "price": round(price, 2),
                        "cost_basis": round(cost_basis, 2) if cost_basis else None,
                        "current_value": round(market_value, 2),
                        "unrealized_gl": round(unrealized_gl, 2),
                    })
                except (ValueError, TypeError, AttributeError) as e:
                    logger.debug("Error parsing position: %s", e)
                    continue

            # Parse cash sweep positions
            cash_summary = api_response.get("cashSweepPositionSummary", {})
            for pos in cash_summary.get("positions", []):
                try:
                    market_value = float(pos.get("marketValue", {}).get("baseValueAmount", 0))
                    if market_value <= 0:
                        continue

                    holdings.append({
                        "symbol": "CASH",
                        "quantity": market_value,
                        "price": 1.0,
                        "cost_basis": round(market_value, 2),
                        "current_value": round(market_value, 2),
                        "unrealized_gl": 0.0,
                    })
                except (ValueError, TypeError, AttributeError) as e:
                    logger.debug("Error parsing cash position: %s", e)
                    continue

        except Exception as e:
            broker_event(
                f"Error fetching holdings for account {account_id}: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
            traceback.print_exc()

        return holdings

    async def _find_button(self, selectors, text_matches):
        """Find a button by CSS selectors, falling back to text content match."""
        for selector in selectors:
            try:
                button = await self._page.select(selector, timeout=2)
                if button:
                    return button
            except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                continue

        # Fallback: find by text content
        conditions = " || ".join(
            f"btn.textContent.toLowerCase().includes('{t}')" for t in text_matches
        )
        return await self._page.evaluate(f"""
            Array.from(document.querySelectorAll('button')).find(btn => {conditions})
        """)

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
            account_ids = await self._get_account_ids()

            if not account_ids:
                broker_event("No Chase accounts found", level="warning", logger=logger)
                return None

            broker_event(f"Fetching holdings for {len(account_ids)} account(s)", logger=logger)

            for account_id in account_ids:
                holdings = await self._fetch_holdings_json(account_id)

                # Filter by ticker if specified
                if ticker:
                    holdings = [
                        h for h in holdings if h["symbol"].upper() == ticker.upper()
                    ]

                if holdings:
                    all_holdings[account_id] = holdings
                    broker_event(
                        f"Account {account_id}: {len(holdings)} position(s)",
                        logger=logger,
                    )

        except Exception as e:
            broker_event(
                f"Error getting holdings: {e}",
                level="error",
                logger=logger,
                exc=e,
            )
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
        account_ids = await self._get_account_ids()
        if not account_ids:
            broker_event("No Chase accounts found for trading", level="error", logger=logger)
            return False

        # Build name map from account metadata
        account_name_map = {}
        if self._accounts:
            for acct in self._accounts:
                account_name_map[acct["id"]] = acct.get("name") or acct["id"]

        success_count = 0

        for account_id in account_ids:
            account_name = account_name_map.get(account_id) or account_id
            broker_event(f"Trading on: {account_name}", logger=logger)

            try:
                # Navigate to trade page
                await self._page.get(
                    "https://secure.chase.com/web/auth/dashboard#/dashboard/investments/youInvest/trade"
                )
                await asyncio.sleep(3)

                # Enter symbol
                broker_event(f"Entering symbol: {ticker}", logger=logger)
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
                broker_event(f"Setting action to {action_text.upper()}", logger=logger)

                # Click buy or sell button
                action_selector = f"button[data-action={action_text}], button:contains('{action_text.capitalize()}')"
                try:
                    action_button = await self._page.select(action_selector, timeout=5)
                    await action_button.click()
                    await asyncio.sleep(1)
                except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
                    logger.debug("Could not find %s button with selector %s", action_text, action_selector)
                    # Try JavaScript click
                    await self._page.evaluate(f"""
                        Array.from(document.querySelectorAll('button')).find(btn => 
                            btn.textContent.toLowerCase().includes('{action_text}')
                        )?.click()
                    """)
                    await asyncio.sleep(1)

                # Enter quantity
                broker_event(f"Setting quantity to {qty}", logger=logger)
                qty_input = await self._page.select(
                    "input[id=quantity], input[name=quantity]", timeout=5
                )
                await qty_input.click()
                await qty_input.clear_input()
                await qty_input.send_keys(str(qty))
                await asyncio.sleep(1)

                # Set order type (Market or Limit)
                if price:
                    broker_event(f"Setting limit price to ${price}", logger=logger)
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
                    broker_event("Using market order", logger=logger)
                    market_button = await self._page.select(
                        "button[data-order-type=market], input[value=MARKET]", timeout=5
                    )
                    await market_button.click()
                    await asyncio.sleep(1)

                # Check for dry run
                dry_run = os.getenv("DRY_RUN", "false").lower() == "true"
                if dry_run:
                    broker_event(
                        f"[DRY RUN] Would {side} {qty} shares of {ticker} on {account_name}",
                        logger=logger,
                    )
                    success_count += 1
                    continue

                # Click preview order button
                broker_event("Clicking preview/review order...", logger=logger)
                preview_button = await self._find_button(
                    ["button[id=preview-order]", "button[id=review-order]",
                     "button:contains('Preview')", "button:contains('Review')"],
                    ["preview", "review"],
                )

                if preview_button:
                    await preview_button.click()
                    await asyncio.sleep(3)
                else:
                    broker_event("Could not find preview button", level="warning", logger=logger)
                    continue

                # Check for errors on preview page
                page_text = await self._page.evaluate("document.body.textContent")
                if "error" in page_text.lower() and "warning" not in page_text.lower():
                    broker_event(f"Error on preview page for {account_name}", level="warning", logger=logger)
                    continue

                # Click submit/place order button
                broker_event("Placing order...", logger=logger)
                submit_button = await self._find_button(
                    ["button[id=submit-order]", "button[id=place-order]",
                     "button:contains('Submit')", "button:contains('Place Order')"],
                    ["submit", "place order"],
                )

                if submit_button:
                    await submit_button.click()
                    await asyncio.sleep(3)
                else:
                    broker_event("Could not find submit button", level="warning", logger=logger)
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

        session_manager.sessions["chase"] = {
            "username": CHASE_USER,
            "password": CHASE_PASS,
        }
        broker_event("✓ Chase credentials loaded", logger=logger)
        session_manager._initialized.add("chase")

    return session_manager.sessions.get("chase")
