"""
Base infrastructure for broker integrations.

This module contains shared utilities, session management, and configuration
used across all broker implementations.
"""

import httpx
import asyncio
import time
import traceback
from typing import Dict, Optional, Any, ClassVar
from dotenv import load_dotenv

load_dotenv("./.env")

# Global HTTP client with connection pooling
http_client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    http2=True
)

# Retry configuration
RETRY_ATTEMPTS = 3
RETRY_DELAY = 1.0  # seconds

# Rate limiting configuration
RATE_LIMIT_DELAY = 0.1  # 100ms between API calls per broker
RATE_LIMIT_WINDOW = 1.0  # 1 second window for rate limiting


class RateLimiter:
    """Rate limiter for broker API calls with per-broker limits."""

    # Per-broker rate limits (requests per second)
    BROKER_LIMITS = {
        "Robinhood": 5,      # Conservative limit
        "Tradier": 2,        # 120 per minute = 2 per second
        "TastyTrade": 10,    # Reasonable default
        "Public": 20,        # Higher limit
        "Firstrade": 5,      # Conservative
        "Fennel": 10,        # Reasonable default
        "Schwab": 5,         # Conservative
        "BBAE": 5,           # Conservative
        "DSPAC": 5,          # Conservative
        "SoFi": 5,           # Conservative
        "Webull": 5,         # Conservative
        "WellsFargo": 5,     # Conservative
    }

    def __init__(self):
        self.last_call_time = {}

    async def wait_if_needed(self, broker_name: str):
        """Wait if necessary to respect per-broker rate limits."""
        # Get broker-specific limit or use default of 10 req/sec
        calls_per_second = self.BROKER_LIMITS.get(broker_name, 10)
        min_interval = 1.0 / calls_per_second

        current_time = time.time()
        last_call = self.last_call_time.get(broker_name, 0)

        time_since_last = current_time - last_call
        if time_since_last < min_interval:
            wait_time = min_interval - time_since_last
            await asyncio.sleep(wait_time)

        self.last_call_time[broker_name] = time.time()


# Global rate limiter
rate_limiter = RateLimiter()


class APICache:
    """Simple in-memory cache for API responses."""

    def __init__(self, max_size=1000, ttl=300):  # 5 minutes TTL
        self.max_size = max_size
        self.ttl = ttl
        self._cache = {}
        self._timestamps = {}

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        if key in self._cache:
            if time.time() - self._timestamps[key] < self.ttl:
                return self._cache[key]
            else:
                # Expired, remove
                del self._cache[key]
                del self._timestamps[key]
        return None

    def set(self, key: str, value: Any):
        """Set cached value with timestamp."""
        # Simple LRU eviction
        if len(self._cache) >= self.max_size:
            # Remove oldest entry
            oldest_key = min(self._timestamps.keys(), key=lambda k: self._timestamps[k])
            del self._cache[oldest_key]
            del self._timestamps[oldest_key]

        self._cache[key] = value
        self._timestamps[key] = time.time()

    def clear(self):
        """Clear all cached data."""
        self._cache.clear()
        self._timestamps.clear()


# Global API cache
api_cache = APICache()


class BrokerConfig:
    """Centralized broker configuration to eliminate duplication."""

    BROKERS: ClassVar[Dict[str, Dict[str, Any]]] = {
        "Robinhood": {
            "session_key": "robinhood",
            "env_vars": ["ROBINHOOD_USER", "ROBINHOOD_PASS", "ROBINHOOD_MFA"],
            "requires_mfa": True,
            "enabled": True
        },
        "Tradier": {
            "session_key": "tradier",
            "env_vars": ["TRADIER_ACCESS_TOKEN"],
            "requires_mfa": False,
            "enabled": True
        },
        "TastyTrade": {
            "session_key": "tastytrade",
            "env_vars": ["TASTY_USER", "TASTY_PASS"],
            "requires_mfa": False,
            "enabled": True
        },
        "Public": {
            "session_key": "public",
            "env_vars": ["PUBLIC_API_SECRET"],
            "requires_mfa": False,
            "enabled": True
        },
        "Firstrade": {
            "session_key": "firstrade",
            "env_vars": ["FIRSTRADE_USER", "FIRSTRADE_PASS", "FIRSTRADE_MFA"],
            "requires_mfa": True,
            "enabled": True
        },
        "Fennel": {
            "session_key": "fennel",
            "env_vars": ["FENNEL_ACCESS_TOKEN"],
            "requires_mfa": False,
            "enabled": True
        },
        "Schwab": {
            "session_key": "schwab",
            "env_vars": ["SCHWAB_API_KEY", "SCHWAB_API_SECRET", "SCHWAB_CALLBACK_URL", "SCHWAB_TOKEN_PATH"],
            "requires_mfa": False,
            "enabled": True
        },
        "BBAE": {
            "session_key": "bbae",
            "env_vars": ["BBAE_USER", "BBAE_PASS"],
            "requires_mfa": False,
            "enabled": True
        },
        "DSPAC": {
            "session_key": "dspac",
            "env_vars": ["DSPAC_USER", "DSPAC_PASS"],
            "requires_mfa": False,
            "enabled": True
        },
        "SoFi": {
            "session_key": "sofi",
            "env_vars": ["SOFI_USER", "SOFI_PASS"],
            "requires_mfa": False,
            "enabled": True
        },
        "Webull": {
            "session_key": "webull",
            "env_vars": ["WEBULL_ACCESS_TOKEN", "WEBULL_REFRESH_TOKEN", "WEBULL_UUID", "WEBULL_ACCOUNT_ID","WEBULL_DID"],
            "requires_mfa": False,
            "enabled": True
        },
        "WellsFargo": {
            "session_key": "wellsfargo",
            "env_vars": ["WELLSFARGO_USER", "WELLSFARGO_PASS"],
            "requires_mfa": False,
            "enabled": True
        }
    }

    @classmethod
    def get_broker_info(cls, broker_name: str) -> Optional[Dict[str, Any]]:
        """Get broker configuration information."""
        return cls.BROKERS.get(broker_name)

    @classmethod
    def get_all_brokers(cls) -> list:
        """Get list of all enabled brokers."""
        return [name for name, config in cls.BROKERS.items() if config["enabled"]]

    @classmethod
    def get_session_key(cls, broker_name: str) -> Optional[str]:
        """Get session key for a broker."""
        config = cls.get_broker_info(broker_name)
        return config["session_key"] if config else None

    @classmethod
    def get_env_vars(cls, broker_name: str) -> list:
        """Get required environment variables for a broker."""
        config = cls.get_broker_info(broker_name)
        return config["env_vars"] if config else []


class RetryableError(Exception):
    """Custom exception for retryable operations."""
    pass


async def retry_operation(operation, max_attempts=RETRY_ATTEMPTS, delay=RETRY_DELAY):
    """Retry an operation with exponential backoff."""
    last_exception = None

    for attempt in range(max_attempts):
        try:
            return await operation()
        except Exception as e:
            last_exception = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay * (2 ** attempt))  # Exponential backoff
            continue

    if last_exception:
        raise last_exception
    else:
        raise Exception("Retry operation failed")


async def _login_broker(broker_api, broker_name):
    """Helper function to handle login flow for BBAE and DSPAC brokers"""
    try:
        broker_api.make_initial_request()
        login_ticket = broker_api.generate_login_ticket_email()

        if login_ticket.get("Data") is None:
            raise Exception("Invalid response from generating login ticket")

        if login_ticket.get("Data").get("needSmsVerifyCode", False):
            if login_ticket.get("Data").get("needCaptchaCode", False):
                captcha_image = broker_api.request_captcha()
                captcha_image.save(f"./{broker_name}captcha.png", format="PNG")
                captcha_input = input(
                    f"CAPTCHA image saved to ./{broker_name}captcha.png. Please open it and type in the code: "
                )
                broker_api.request_email_code(captcha_input=captcha_input)
            else:
                broker_api.request_email_code()

            otp_code = input(f"Enter {broker_name} security code: ")
            login_ticket = broker_api.generate_login_ticket_email(otp_code)

        login_response = broker_api.login_with_ticket(login_ticket.get("Data").get("ticket"))
        if login_response.get("Outcome") != "Success":
            raise Exception(f"Login failed. Response: {login_response}")

        return True

    except Exception as e:
        print(f"Error logging into {broker_name}: {e}")
        return False


async def _get_broker_holdings(broker_api, broker_name, ticker=None):
    """Helper function to get holdings for BBAE and DSPAC brokers"""
    try:
        holdings_data = {}
        holdings_response = broker_api.get_account_holdings()

        if holdings_response.get("Outcome") != "Success":
            raise Exception(f"Failed to get holdings: {holdings_response.get('Message')}")

        positions = holdings_response.get("Data", [])

        if ticker:
            positions = [pos for pos in positions if pos.get("Symbol") == ticker]

        account_info = broker_api.get_account_info()
        account_number = account_info.get("Data").get("accountNumber")

        formatted_positions = [
            {
                "symbol": pos.get("Symbol", "Unknown"),
                "quantity": float(pos.get("CurrentAmount", 0)),
                "cost_basis": float(pos.get("CostPrice", 0)),
                "current_value": float(pos.get("Last", 0)) * float(pos.get("CurrentAmount", 0))
            }
            for pos in positions
            if float(pos.get("CurrentAmount", 0)) > 0
        ]

        holdings_data[account_number] = formatted_positions
        return holdings_data

    except Exception as e:
        print(f"Error retrieving {broker_name} holdings: {e}")
        traceback.print_exc()
        return None
