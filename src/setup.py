import os
import secrets
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, urlencode
from dotenv import load_dotenv

load_dotenv("./.env")

TASTYTRADE_OAUTH_APP_URL = "https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications"
TASTYTRADE_AUTH_URL = "https://my.tastytrade.com/auth.html"
TASTYTRADE_TOKEN_URL = "https://api.tastyworks.com/oauth/token"
TASTYTRADE_REDIRECT_URI = "http://localhost:8000"
TASTYTRADE_SCOPES = "read trade"


def validate_credentials(service, credentials):
    """Validate that required credentials are provided."""
    missing = []
    for env_var, prompt in credentials:
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
            self.wfile.write(b"<html><body><h2>Missing code parameter.</h2></body></html>")

    def log_message(self, format, *args):
        pass  # Suppress request logging


def _exchange_code_for_tokens(client_id, client_secret, code):
    """Exchange an authorization code for access + refresh tokens.

    Returns:
        dict with 'access_token', 'refresh_token', 'expires_in' on success.
    Raises:
        Exception on failure.
    """
    import httpx

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

    open_browser = input("Open TastyTrade OAuth apps page in browser? (Y/n): ").strip().lower()
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
    auth_params = urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": TASTYTRADE_REDIRECT_URI,
        "scope": TASTYTRADE_SCOPES,
        "state": state,
    })
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
        from tastytrade import Session

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
            (
                "WEBULL_ACCESS_TOKEN",
                "Access Token (from Chrome extension - RECOMMENDED)",
            ),
            ("WEBULL_REFRESH_TOKEN", "Refresh Token (from Chrome extension)"),
            ("WEBULL_UUID", "UUID (from Chrome extension)"),
            ("WEBULL_ACCOUNT_ID", "Account ID (from Chrome extension)"),
            ("WEBULL_DID", "Device ID (optional)"),
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
