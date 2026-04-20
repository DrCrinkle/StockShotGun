import os
import json
import asyncio
import secrets
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from dotenv import load_dotenv

load_dotenv("./.env")

TASTYTRADE_OAUTH_APP_URL = (
    "https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications"
)
TASTYTRADE_AUTH_URL = "https://my.tastytrade.com/auth.html"
TASTYTRADE_TOKEN_URL = "https://api.tastyworks.com/oauth/token"
TASTYTRADE_REDIRECT_URI = "http://localhost:8000"
TASTYTRADE_SCOPES = "read trade"
WEBULL_LOGIN_URL = (
    "https://passport.webull.com/auth/simple/login?redirect_uri="
    "https%3A%2F%2Fapp.webull.com%2Fwatch"
)


def _collect_webull_fields(obj, found):
    """Recursively collect likely Webull auth fields from nested data."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            normalized_key = str(key).lower()

            if isinstance(value, str) and value:
                if "refresh" in normalized_key and "token" in normalized_key:
                    found["refresh_tokens"].append(value)
                elif "access" in normalized_key and "token" in normalized_key:
                    found["access_tokens"].append(value)
                elif normalized_key in {"uuid", "useruuid", "user_uuid"}:
                    found["uuids"].append(value)
                elif normalized_key in {"did", "deviceid", "device_id", "device-id", "eid"}:
                    found["device_ids"].append(value)
                elif "account" in normalized_key and "id" in normalized_key:
                    found["account_ids"].append(value)

            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
                if parsed is not None:
                    _collect_webull_fields(parsed, found)
            else:
                _collect_webull_fields(value, found)

    elif isinstance(obj, list):
        for item in obj:
            _collect_webull_fields(item, found)


def _dedupe_preserve_order(values):
    """Return values with duplicates and blanks removed, preserving order."""
    result = []
    seen = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _pick_best(values):
    """Pick the first non-empty value from candidate values."""
    deduped = _dedupe_preserve_order(values)
    return deduped[0] if deduped else ""


async def _try_webull_api_account_discovery(
    access_token, refresh_token, uuid, device_id
):
    """Attempt account discovery through Webull api_login using captured tokens.

    Returns:
        dict with keys:
            "account_ids": list of discovered account ID strings
            "device_id":   DID resolved by the webull library after login (may be
                           the same as the input device_id, or the library default
                           if none was supplied)
    """
    empty = {"account_ids": [], "device_id": device_id or ""}
    if not all([access_token, refresh_token, uuid]):
        return empty

    try:
        import importlib

        webull_module = importlib.import_module("webull")
        discover_module = importlib.import_module("brokers.webull")
        webull_factory = getattr(webull_module, "webull", None)
        discover_accounts_fn = getattr(discover_module, "_discover_accounts", None)
        if webull_factory is None or discover_accounts_fn is None:
            return empty
    except Exception:
        return empty

    try:
        wb = webull_factory()
        if device_id:
            did_setter = getattr(wb, "set_did", None)
            if did_setter is None:
                did_setter = getattr(wb, "_set_did", None)
            if did_setter is not None:
                await __import__("asyncio").to_thread(did_setter, device_id)
            elif hasattr(wb, "_did"):
                wb._did = device_id

        await __import__("asyncio").to_thread(
            wb.api_login,
            access_token=access_token,
            refresh_token=refresh_token,
            token_expire="2099-01-01T00:00:00.000+0000",
            uuid=uuid,
        )

        # Read the DID the library resolved after login — use as fallback if
        # the browser capture didn't surface one.
        resolved_did = device_id or ""
        try:
            lib_did = str(getattr(wb, "_did", "") or "").strip()
            if lib_did:
                resolved_did = lib_did
        except Exception:
            pass

        discovered = await discover_accounts_fn(wb, start_index=0, max_accounts=11)
        account_ids = _dedupe_preserve_order(
            [
                entry.get("account_id", "")
                for entry in discovered
                if isinstance(entry, dict)
            ]
        )
        return {"account_ids": account_ids, "device_id": resolved_did}
    except Exception:
        return empty


async def _capture_webull_tokens_with_zendriver(username, password):
    """Capture Webull tokens via CDP Network interception (like the Chrome extension).

    Uses CDP Network.enable to intercept request headers (for DID) and response
    bodies (for access/refresh tokens, UUID, and account IDs). This works across
    cross-origin redirects unlike JS-based approaches.

    Falls back to scanning localStorage/sessionStorage/IndexedDB as a supplement.
    """
    import asyncio
    from zendriver.cdp import network as cdp_network
    from brokers.browser_utils import create_browser, stop_browser, wait_for_ready_state

    browser = None
    previous_browser_path = os.getenv("BROWSER_PATH")
    os.environ["BROWSER_PATH"] = "/usr/bin/helium-browser"

    # Shared state for CDP event handlers
    found = {
        "access_tokens": [],
        "refresh_tokens": [],
        "uuids": [],
        "device_ids": [],
        "account_ids": [],
    }
    # Track request IDs we want to fetch response bodies for
    pending_responses = {}

    try:
        browser = await create_browser(headless=False)
        page = await browser.get(WEBULL_LOGIN_URL)
        await wait_for_ready_state(page, timeout=20)

        # Enable CDP Network domain for request/response interception
        await page.send(cdp_network.enable())

        # --- CDP Event Handlers (mirror the Chrome extension approach) ---

        async def _on_request(event: cdp_network.RequestWillBeSent):
            """Capture DID header from webullfintech.com requests."""
            url = str(event.request.url or "")
            if "webullfintech.com" not in url:
                return
            headers = event.request.headers or {}
            # headers is a cdp Headers object (dict-like)
            for key in headers:
                if key.lower() == "did":
                    did_value = str(headers[key]).strip()
                    if did_value:
                        found["device_ids"].append(did_value)
                        print(f"  ↳ CDP captured DID: {did_value[:16]}…")
                    break

        async def _on_response(event: cdp_network.ResponseReceived):
            """Queue interesting responses for body retrieval."""
            url = str(event.response.url or "")
            request_id = event.request_id

            if ("passport/login/v3/login" in url or
                    "/api/user/v1/login/account" in url):
                pending_responses[request_id] = "tokens"
            elif "/api/trading/v1/global/tradetab/display" in url:
                pending_responses[request_id] = "accounts"

        async def _on_loading_finished(event: cdp_network.LoadingFinished):
            """Fetch response bodies for queued requests."""
            request_id = event.request_id
            response_type = pending_responses.pop(request_id, None)
            if not response_type:
                return

            try:
                body_result = await page.send(
                    cdp_network.get_response_body(request_id)
                )
                body_str = body_result[0] if isinstance(body_result, tuple) else str(body_result)
                data = json.loads(body_str)

                if response_type == "tokens" and data.get("accessToken"):
                    found["access_tokens"].append(str(data["accessToken"]))
                    if data.get("refreshToken"):
                        found["refresh_tokens"].append(str(data["refreshToken"]))
                    if data.get("uuid"):
                        found["uuids"].append(str(data["uuid"]))
                    print("  ↳ CDP captured tokens from login response")

                elif response_type == "accounts" and data.get("accountList"):
                    for acc in data["accountList"]:
                        sec_id = acc.get("secAccountId")
                        if sec_id:
                            found["account_ids"].append(str(sec_id))
                    print(f"  ↳ CDP captured {len(data['accountList'])} account(s)")

            except Exception as e:
                print(f"  ↳ CDP response body fetch failed: {e}")

        page.add_handler(cdp_network.RequestWillBeSent, _on_request)
        page.add_handler(cdp_network.ResponseReceived, _on_response)
        page.add_handler(cdp_network.LoadingFinished, _on_loading_finished)

        # --- Auto-fill login form if credentials provided ---
        if username and password:
            fill_result = await page.evaluate(
                """
                (() => {
                    const findOne = (selectors) => {
                        for (const selector of selectors) {
                            const node = document.querySelector(selector);
                            if (node) return node;
                        }
                        return null;
                    };

                    const usernameSelectors = [
                        'input[name="username"]',
                        'input[name="email"]',
                        'input[type="email"]',
                        'input[autocomplete="username"]',
                        'input[type="text"]'
                    ];
                    const passwordSelectors = [
                        'input[name="password"]',
                        'input[type="password"]',
                        'input[autocomplete="current-password"]'
                    ];

                    const userInput = findOne(usernameSelectors);
                    const passInput = findOne(passwordSelectors);

                    if (userInput) {
                        userInput.focus();
                        userInput.value = __USERNAME__;
                        userInput.dispatchEvent(new Event('input', { bubbles: true }));
                        userInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }

                    if (passInput) {
                        passInput.focus();
                        passInput.value = __PASSWORD__;
                        passInput.dispatchEvent(new Event('input', { bubbles: true }));
                        passInput.dispatchEvent(new Event('change', { bubbles: true }));
                    }

                    const submitSelectors = [
                        'button[type="submit"]',
                        'button[data-testid*="login"]',
                        'button[class*="login"]'
                    ];

                    let submitButton = findOne(submitSelectors);
                    if (!submitButton) {
                        submitButton = Array.from(document.querySelectorAll('button')).find((button) => {
                            const text = (button.innerText || '').toLowerCase();
                            return text.includes('log in') || text.includes('sign in');
                        }) || null;
                    }

                    if (submitButton) {
                        submitButton.click();
                    }

                    return {
                        userInputFound: Boolean(userInput),
                        passInputFound: Boolean(passInput),
                        submitFound: Boolean(submitButton)
                    };
                })()
                """.replace("__USERNAME__", json.dumps(username)).replace(
                    "__PASSWORD__", json.dumps(password)
                )
            )

            if fill_result and fill_result.get("submitFound"):
                print(
                    "Webull login form submitted. Complete any MFA/CAPTCHA in the browser."
                )
            else:
                print("Could not confidently submit login form automatically.")
                print(
                    "Please complete Webull login manually in the opened browser window."
                )
        else:
            print("No Webull username/password provided.")
            print("Please complete Webull login manually in the opened browser window.")

        print("Waiting up to 2 minutes for login (CDP network capture active)...")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 120
        stable_polls = 0
        last_counts = None

        while loop.time() < deadline:
            # Re-register CDP handlers on new tabs after cross-origin redirect
            try:
                await browser.update_targets()
                current_tab = browser.main_tab
                if current_tab is not None and current_tab != page:
                    page = current_tab
                    await page.send(cdp_network.enable())
                    page.add_handler(cdp_network.RequestWillBeSent, _on_request)
                    page.add_handler(cdp_network.ResponseReceived, _on_response)
                    page.add_handler(cdp_network.LoadingFinished, _on_loading_finished)
            except Exception:
                pass

            counts = (
                len(found["access_tokens"]),
                len(found["refresh_tokens"]),
                len(found["uuids"]),
                len(found["device_ids"]),
                len(found["account_ids"]),
            )

            if counts == last_counts:
                stable_polls += 1
            else:
                stable_polls = 0
                last_counts = counts

            has_access = bool(_pick_best(found["access_tokens"]))
            has_refresh = bool(_pick_best(found["refresh_tokens"]))
            has_uuid = bool(_pick_best(found["uuids"]))
            has_did = bool(_pick_best(found["device_ids"]))
            has_account = bool(_dedupe_preserve_order(found["account_ids"]))

            if has_access and has_refresh and has_uuid and has_did and has_account:
                print("✓ All credentials captured via CDP")
                break

            if has_access and has_refresh and has_uuid:
                # Core tokens captured — accounts/DID can be discovered via API
                if stable_polls >= 3:
                    print("✓ Core tokens captured, will discover accounts via API")
                    break

            await asyncio.sleep(2)

        access_token = _pick_best(found["access_tokens"])
        refresh_token = _pick_best(found["refresh_tokens"])
        uuid = _pick_best(found["uuids"])
        device_id = _pick_best(found["device_ids"])
        account_ids = _dedupe_preserve_order(found["account_ids"])

        api_discovery = await _try_webull_api_account_discovery(
            access_token,
            refresh_token,
            uuid,
            device_id,
        )

        for discovered in api_discovery.get("account_ids", []):
            if discovered not in account_ids:
                account_ids.append(discovered)

        if not device_id:
            device_id = api_discovery.get("device_id", "")
            if device_id:
                print(f"  ↳ DID not found in network; using library-resolved DID: {device_id[:16]}…")

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "uuid": uuid,
            "device_id": device_id,
            "account_ids": account_ids,
        }
    finally:
        if previous_browser_path is None:
            os.environ.pop("BROWSER_PATH", None)
        else:
            os.environ["BROWSER_PATH"] = previous_browser_path
        await stop_browser(browser)


def _run_async_from_sync(async_fn, *args):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_fn(*args))

    result_holder = {}
    error_holder = {}

    def _runner():
        try:
            result_holder["result"] = asyncio.run(async_fn(*args))
        except Exception as exc:
            error_holder["error"] = exc

    worker = threading.Thread(target=_runner, daemon=True)
    worker.start()
    worker.join()

    if "error" in error_holder:
        raise error_holder["error"]

    return result_holder.get("result")


def _setup_webull_with_zendriver_capture():
    """Optional best-effort Webull token capture path using zendriver."""

    print("Webull token capture (optional):")
    print("  This attempts to read tokens from your logged-in browser session.")
    print("  It is best-effort and may fail due to MFA/CAPTCHA/Webull page changes.")

    should_try = input("Try Zendriver token capture now? (y/N): ").strip().lower()
    if should_try != "y":
        return

    username = input(
        "Webull username/email (or ENTER for manual browser login): "
    ).strip() or os.getenv("WEBULL_USER", "")
    password = ""
    if username:
        password = input(
            "Webull password (or ENTER for manual browser login): "
        ).strip() or os.getenv("WEBULL_PASS", "")

    try:
        captured = (
            _run_async_from_sync(
                _capture_webull_tokens_with_zendriver, username, password
            )
            or {}
        )
    except Exception as exc:
        print(f"Zendriver capture failed: {exc}")
        print("Falling back to manual Webull token entry.")
        return

    if captured.get("access_token"):
        os.environ["WEBULL_ACCESS_TOKEN"] = captured["access_token"]
    if captured.get("refresh_token"):
        os.environ["WEBULL_REFRESH_TOKEN"] = captured["refresh_token"]
    if captured.get("uuid"):
        os.environ["WEBULL_UUID"] = captured["uuid"]
    if captured.get("device_id"):
        os.environ["WEBULL_DID"] = captured["device_id"]
    if captured.get("account_ids"):
        os.environ["WEBULL_ACCOUNT_ID"] = ",".join(captured["account_ids"])

    required = [
        os.getenv("WEBULL_ACCESS_TOKEN"),
        os.getenv("WEBULL_REFRESH_TOKEN"),
        os.getenv("WEBULL_UUID"),
    ]
    if all(required):
        print("✓ Captured core Webull token fields.")
        if os.getenv("WEBULL_ACCOUNT_ID"):
            print("✓ Captured/discovered Webull account ID(s).")
        else:
            print(
                "ℹ No account IDs discovered yet; runtime will auto-discover accounts."
            )
    else:
        print("⚠ Capture was incomplete. Continue with manual values below.")


def _load_webull_profiles_env():
    raw_profiles = os.getenv("WEBULL_PROFILES") or os.getenv("SSG_WEBULL_PROFILES")
    if not raw_profiles:
        return []

    try:
        parsed = json.loads(raw_profiles)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []

    if isinstance(parsed, dict):
        parsed = parsed.get("profiles", [])

    if not isinstance(parsed, list):
        return []

    normalized_profiles = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        normalized_profiles.append(item)
    return normalized_profiles


def _prompt_webull_profile(default_name):
    print()
    profile_name = (
        input(f"Webull profile name [{default_name}]: ").strip() or default_name
    )

    print("Webull token capture (optional):")
    print("  This attempts to read tokens from your logged-in browser session.")
    print("  It is best-effort and may fail due to MFA/CAPTCHA/Webull page changes.")

    captured = {}
    use_captured_values = False
    should_try = input("Try Zendriver token capture now? (y/N): ").strip().lower()
    if should_try == "y":
        username = input(
            "Webull username/email (or ENTER for manual browser login): "
        ).strip()
        password = ""
        if username:
            password = input(
                "Webull password (or ENTER for manual browser login): "
            ).strip()

        try:
            captured = (
                _run_async_from_sync(
                    _capture_webull_tokens_with_zendriver,
                    username,
                    password,
                )
                or {}
            )
            if (
                captured.get("access_token")
                and captured.get("refresh_token")
                and captured.get("uuid")
            ):
                print("✓ Captured core Webull token fields.")
                keep_captured = (
                    input("Use captured token values without re-entering them? (Y/n): ")
                    .strip()
                    .lower()
                )
                use_captured_values = keep_captured != "n"
            else:
                print("⚠ Capture incomplete. Please fill fields manually below.")
        except Exception as exc:
            print(f"Zendriver capture failed: {exc}")
            print("Continuing with manual profile entry.")

    def _prefilled(prompt_text, current_value):
        if current_value:
            print(f"{prompt_text}: [existing value hidden] (press ENTER to keep)")
            return (
                input(f"New {prompt_text} (or ENTER to keep existing): ").strip()
                or current_value
            )
        return input(f"{prompt_text}: ").strip()

    def _captured_or_prompt(prompt_text, current_value):
        if use_captured_values and current_value:
            print(f"{prompt_text}: [captured value accepted]")
            return current_value
        return _prefilled(prompt_text, current_value)

    access_token = _captured_or_prompt(
        "Webull Access Token", str(captured.get("access_token", ""))
    )
    refresh_token = _captured_or_prompt(
        "Webull Refresh Token", str(captured.get("refresh_token", ""))
    )
    uuid = _captured_or_prompt("Webull UUID", str(captured.get("uuid", "")))

    captured_accounts = captured.get("account_ids") or []
    account_default = ",".join(
        [str(item).strip() for item in captured_accounts if item]
    )
    account_ids_raw = _captured_or_prompt(
        "Webull Account ID(s), comma-separated (optional)", account_default
    )
    account_ids = [item.strip() for item in account_ids_raw.split(",") if item.strip()]

    device_id = _captured_or_prompt(
        "Webull Device ID (optional)", str(captured.get("device_id", ""))
    )
    if not device_id:
        print(
            "  ⚠ No Device ID captured. Orders may fail with 'new device' errors.\n"
            "    Run setup again and use Zendriver capture, or paste the DID from\n"
            "    DevTools → Application → Local Storage (key: 'did' or 'eid') on app.webull.com."
        )
    trading_pin = input("Webull Trading PIN (optional for placing orders): ").strip()
    username_saved = input("Webull Username/email (optional): ").strip()
    password_saved = input("Webull Password (optional): ").strip()

    profile = {
        "name": profile_name,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "uuid": uuid,
        "account_ids": account_ids,
        "device_id": device_id,
    }
    if trading_pin:
        profile["trading_pin"] = trading_pin
    if username_saved:
        profile["username"] = username_saved
    if password_saved:
        profile["password"] = password_saved

    return profile


def _setup_webull_profiles_interactive():
    print("Webull multi-profile setup (optional):")
    print("  Use this to add multiple Webull logins into WEBULL_PROFILES.")

    enable_multi = input("Configure Webull profiles mode? (Y/n): ").strip().lower()
    if enable_multi == "n":
        return False

    profiles = _load_webull_profiles_env()
    if profiles:
        print(f"Found {len(profiles)} existing profile(s) in WEBULL_PROFILES.")
        keep_existing = (
            input("Keep existing profiles and append new ones? (Y/n): ").strip().lower()
        )
        if keep_existing == "n":
            profiles = []

    while True:
        default_name = f"profile-{len(profiles) + 1}"
        profile = _prompt_webull_profile(default_name)
        if (
            profile.get("access_token")
            and profile.get("refresh_token")
            and profile.get("uuid")
        ):
            profiles.append(profile)
            print(f"✓ Added Webull profile '{profile.get('name')}'")
        else:
            print("⚠ Profile missing required token fields; skipping.")

        add_another = input("Add another Webull profile? (y/N): ").strip().lower()
        if add_another != "y":
            break

    if not profiles:
        print("No valid Webull profiles were configured.")
        return True

    os.environ["WEBULL_PROFILES"] = json.dumps(profiles, separators=(",", ":"))

    first = profiles[0]
    if first.get("access_token"):
        os.environ["WEBULL_ACCESS_TOKEN"] = first["access_token"]
    if first.get("refresh_token"):
        os.environ["WEBULL_REFRESH_TOKEN"] = first["refresh_token"]
    if first.get("uuid"):
        os.environ["WEBULL_UUID"] = first["uuid"]
    if first.get("device_id"):
        os.environ["WEBULL_DID"] = first["device_id"]
    if first.get("account_ids"):
        os.environ["WEBULL_ACCOUNT_ID"] = ",".join(first["account_ids"])

    print(f"✓ Stored {len(profiles)} Webull profile(s) in WEBULL_PROFILES")
    return True


def validate_credentials(service, credentials):
    """Validate that required credentials are provided."""
    if service == "Webull":
        webull_profiles = os.getenv("WEBULL_PROFILES") or os.getenv(
            "SSG_WEBULL_PROFILES"
        )
        if webull_profiles:
            return True

    missing = []
    for env_var, prompt in credentials:
        if "optional" in str(prompt).lower():
            continue
        value = os.getenv(env_var) or os.getenv(f"SSG_{env_var}")
        if not value:
            missing.append(prompt)

    if missing:
        print(f"⚠️  Warning: Missing {service} credentials: {', '.join(missing)}")
        return False
    return True


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler that captures the OAuth authorization code from the callback."""

    auth_code = None
    auth_error = None

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        if "error" in params:
            _OAuthCallbackHandler.auth_error = params["error"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization failed.</h2>"
                b"<p>You can close this tab.</p></body></html>"
            )
        elif "code" in params:
            _OAuthCallbackHandler.auth_code = params["code"][0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Authorization successful!</h2>"
                b"<p>You can close this tab and return to the terminal.</p></body></html>"
            )
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h2>Missing code parameter.</h2></body></html>"
            )

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _exchange_code_for_tokens(client_id, client_secret, code):
    """Exchange an authorization code for access + refresh tokens.

    Returns:
        dict with 'access_token', 'refresh_token', 'expires_in' on success.
    Raises:
        Exception on failure.
    """
    import importlib

    httpx = importlib.import_module("httpx")

    data = {
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": TASTYTRADE_REDIRECT_URI,
    }

    resp = httpx.post(
        TASTYTRADE_TOKEN_URL,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )

    if resp.status_code != 200:
        raise Exception(f"Token exchange failed ({resp.status_code}): {resp.text}")

    return resp.json()


def _setup_tastytrade():
    """Guided OAuth setup for TastyTrade.

    Runs the full OAuth authorization code flow:
    1. User provides client_id and client_secret from their TastyTrade OAuth app
    2. Opens browser to TastyTrade authorization page
    3. Starts a local server to capture the callback with the authorization code
    4. Exchanges the code for tokens
    5. Validates by creating a Session

    Returns:
        True if credentials were set, False if skipped.
    """
    print(f"{'-' * 10}TastyTrade OAuth Setup{'-' * 10}")
    print()

    # Check for existing credentials
    existing_secret = os.getenv("TASTY_CLIENT_SECRET")
    existing_token = os.getenv("TASTY_REFRESH_TOKEN")
    if existing_secret and existing_token:
        print("Existing TastyTrade credentials found.")
        keep = input("Keep existing credentials? (Y/n): ").strip().lower()
        if keep != "n":
            return True

    print("Step 1: Create an OAuth application on TastyTrade (if you haven't already)")
    print(f"  URL: {TASTYTRADE_OAUTH_APP_URL}")
    print("  - Select scopes: 'read' and 'trade'")
    print(f"  - Set callback URL to: {TASTYTRADE_REDIRECT_URI}")
    print("  - Click Create and save both the Client ID and Client Secret")
    print()

    open_browser = (
        input("Open TastyTrade OAuth apps page in browser? (Y/n): ").strip().lower()
    )
    if open_browser != "n":
        webbrowser.open(TASTYTRADE_OAUTH_APP_URL)

    print()
    print("Step 2: Enter your app credentials")
    print()

    client_id = input("TastyTrade Client ID (or ENTER to skip): ").strip()
    if not client_id:
        print("Skipping TastyTrade setup.")
        return False

    client_secret = input("TastyTrade Client Secret (or ENTER to skip): ").strip()
    if not client_secret:
        print("Skipping TastyTrade setup.")
        return False

    # Step 3: Start local server and open authorization URL
    print()
    print("Step 3: Authorize the app")
    print("  A browser window will open for you to log in and authorize.")
    print("  After authorizing, you'll be redirected back here automatically.")
    print()

    state = secrets.token_urlsafe(32)
    auth_params = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": TASTYTRADE_REDIRECT_URI,
            "scope": TASTYTRADE_SCOPES,
            "state": state,
        }
    )
    auth_url = f"{TASTYTRADE_AUTH_URL}?{auth_params}"

    # Reset handler state
    _OAuthCallbackHandler.auth_code = None
    _OAuthCallbackHandler.auth_error = None

    server = HTTPServer(("localhost", 8000), _OAuthCallbackHandler)

    # Run server in background thread so we can wait for the callback
    server_thread = threading.Thread(target=server.handle_request, daemon=True)
    server_thread.start()

    print("Opening browser for authorization...")
    webbrowser.open(auth_url)
    print("Waiting for authorization callback...")

    # Wait for the callback (timeout after 5 minutes)
    server_thread.join(timeout=300)
    server.server_close()

    if _OAuthCallbackHandler.auth_error:
        print(f"Authorization error: {_OAuthCallbackHandler.auth_error}")
        return False

    if not _OAuthCallbackHandler.auth_code:
        print("Timed out waiting for authorization. Please try again.")
        return False

    code = _OAuthCallbackHandler.auth_code
    print("Authorization code received!")

    # Step 4: Exchange code for tokens
    print("Exchanging authorization code for tokens...")
    try:
        token_data = _exchange_code_for_tokens(client_id, client_secret, code)
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return False

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print(f"No refresh token in response: {token_data}")
        return False

    print("Tokens received!")

    # Step 5: Validate by creating a session
    print("Validating credentials...")
    try:
        import importlib

        tastytrade_module = importlib.import_module("tastytrade")
        Session = getattr(tastytrade_module, "Session")

        Session(client_secret, refresh_token)
        print("TastyTrade credentials validated successfully!")
    except Exception as e:
        print(f"Warning: Validation failed ({e}), saving credentials anyway.")

    os.environ["TASTY_CLIENT_ID"] = client_id
    os.environ["TASTY_CLIENT_SECRET"] = client_secret
    os.environ["TASTY_REFRESH_TOKEN"] = refresh_token
    return True


def setup(non_interactive=False, broker_filter=None):
    """Run interactive credential setup.

    Args:
        non_interactive: If True, raise instead of prompting.
        broker_filter: Optional list of broker names to set up (e.g. ["TastyTrade"]).
                       If None, all brokers are offered.
    """
    if non_interactive:
        raise RuntimeError(
            "setup requires interactive input; rerun without --non-interactive"
        )

    all_brokers = {
        "Robinhood": [
            ("ROBINHOOD_USER", "Username"),
            ("ROBINHOOD_PASS", "Password"),
            ("ROBINHOOD_MFA", "MFA"),
        ],
        "Firstrade": [
            ("FIRSTRADE_USER", "Username"),
            ("FIRSTRADE_PASS", "Password"),
            ("FIRSTRADE_MFA", "MFA Secret"),
        ],
        "Schwab": [
            ("SCHWAB_API_KEY", "API Key"),
            ("SCHWAB_API_SECRET", "API Secret"),
            ("SCHWAB_CALLBACK_URL", "Callback URL"),
            ("SCHWAB_TOKEN_PATH", "Token Path"),
        ],
        "Webull": [
            ("WEBULL_ACCESS_TOKEN", "Access Token"),
            ("WEBULL_REFRESH_TOKEN", "Refresh Token"),
            ("WEBULL_UUID", "UUID"),
            (
                "WEBULL_ACCOUNT_ID",
                "Account ID(s), comma-separated (optional; auto-discovered if omitted)",
            ),
            (
                "WEBULL_PROFILES",
                "Profiles JSON (optional, multi-login; overrides single-profile fields)",
            ),
            ("WEBULL_DID", "Device ID (optional)"),
            ("WEBULL_TRADING_PIN", "Trading PIN (optional for placing orders)"),
            ("WEBULL_USER", "Username/email (optional, for Zendriver capture)"),
            ("WEBULL_PASS", "Password (optional, for Zendriver capture)"),
        ],
        "BBAE": [
            ("BBAE_USER", "Username"),
            ("BBAE_PASS", "Password"),
        ],
        "DSPAC": [
            ("DSPAC_USER", "Username"),
            ("DSPAC_PASS", "Password"),
        ],
        "Chase": [
            ("CHASE_USER", "Username"),
            ("CHASE_PASS", "Password"),
            ("CELL_PHONE_LAST_FOUR", "Last four digits of cell phone number"),
        ],
        "SoFi": [
            ("SOFI_USER", "Username"),
            ("SOFI_PASS", "Password"),
            ("SOFI_TOTP", "TOTP Secret (optional, press ENTER to skip)"),
        ],
        "WellsFargo": [
            ("WELLSFARGO_USER", "Username"),
            ("WELLSFARGO_PASS", "Password"),
            (
                "WELLSFARGO_PHONE_SUFFIX",
                "Phone Suffix for 2FA (optional, e.g., last 4 digits)",
            ),
        ],
        "TastyTrade": [
            ("TASTY_CLIENT_ID", "OAuth Client ID"),
            ("TASTY_CLIENT_SECRET", "OAuth Client Secret"),
            ("TASTY_REFRESH_TOKEN", "OAuth Refresh Token"),
        ],
        "Tradier": [("TRADIER_ACCESS_TOKEN", "Access Token")],
        "Public": [("PUBLIC_API_SECRET", "API Secret Key")],
        "Fennel": [
            ("FENNEL_ACCESS_TOKEN", "Personal Access Token (from Fennel Dashboard)")
        ],
    }

    # Filter to specific broker(s) if requested
    if broker_filter:
        invalid = [b for b in broker_filter if b not in all_brokers]
        if invalid:
            print(f"Unknown broker(s): {', '.join(invalid)}")
            print(f"Available: {', '.join(all_brokers.keys())}")
            return
        brokers = {k: v for k, v in all_brokers.items() if k in broker_filter}
        print(f"Setting up credentials for: {', '.join(broker_filter)}")
    else:
        brokers = all_brokers
        print("Setting up broker credentials, press ENTER to skip entry")

    # Check existing credentials first
    print("Checking existing credentials...")
    existing_services = []
    for service, credentials in brokers.items():
        if validate_credentials(service, credentials):
            existing_services.append(service)
            print(f"✓ {service}: Credentials found")
        else:
            print(f"✗ {service}: Credentials missing")

    if existing_services:
        print(f"\nExisting credentials found for: {', '.join(existing_services)}")
        skip_existing = (
            input("Skip setup for existing services? (y/N): ").lower().startswith("y")
        )
    else:
        skip_existing = False

    for service, credentials in brokers.items():
        # Skip if credentials exist and user chose to skip
        if skip_existing and validate_credentials(service, credentials):
            print(f"Skipping {service} (credentials already exist)")
            continue

        # TastyTrade has a special guided OAuth flow
        if service == "TastyTrade":
            _setup_tastytrade()
            continue

        if service == "Webull":
            if _setup_webull_profiles_interactive():
                continue
            _setup_webull_with_zendriver_capture()

        print(f"{'-' * 10}{service}{'-' * 10}")
        for env_var, prompt in credentials:
            # Check for existing value first
            existing_value = os.getenv(env_var) or os.getenv(f"SSG_{env_var}")
            if existing_value:
                print(
                    f"{service} {prompt}: [existing value hidden] (press ENTER to keep)"
                )
                value = (
                    input(f"New {service} {prompt} (or ENTER to keep existing): ")
                    or existing_value
                )
            else:
                value = input(f"{service} {prompt}: ") or ""

            # Store directly without SSG_ prefix to avoid duplication
            if value:
                os.environ[env_var] = value

    print(f"{'-' * 5} Saving credentials to .env {'-' * 5}")

    # Save ALL credentials (not just filtered) to avoid wiping other brokers
    with open(".env", "w") as f:
        for service, credentials in all_brokers.items():
            for env_var, _ in credentials:
                value = os.getenv(env_var)
                if value:
                    f.write(f"{env_var}={value}\n")

    print("Credentials saved to .env")

    # Validate final configuration
    print("\nValidating final configuration...")
    final_validation = []
    for service, credentials in all_brokers.items():
        if validate_credentials(service, credentials):
            final_validation.append(service)

    if final_validation:
        print(
            f"✅ Configuration complete! Services ready: {', '.join(final_validation)}"
        )
    else:
        print("⚠️  No services are fully configured. Please check your .env file.")
