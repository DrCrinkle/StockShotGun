"""
Base infrastructure for broker integrations.

This module contains shared utilities, session management, and configuration
used across all broker implementations.

Free-threaded Python 3.14 Compatibility:
- All shared state is protected by threading.Lock for thread-safety
- Asyncio primitives remain for async coordination
"""

import httpx
import asyncio
import time
import logging
import traceback
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Dict, Optional, Any, ClassVar
from dotenv import load_dotenv
from cli_runtime import CliRuntimeError, ExitCode
from brokers.registry import BROKERS as _REGISTRY

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")

# Global HTTP client with connection pooling
http_client = httpx.AsyncClient(
    timeout=30.0,
    limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
    http2=True,
)

# Retry configuration
RETRY_ATTEMPTS = 3
RETRY_DELAY = 1.0  # seconds

# Rate limiting configuration
RATE_LIMIT_DELAY = 0.1  # 100ms between API calls per broker
RATE_LIMIT_WINDOW = 1.0  # 1 second window for rate limiting


class RateLimiter:
    """Rate limiter for broker API calls with per-broker limits.

    Thread-safe for Free-threaded Python 3.14 (no-GIL).
    """

    # Per-broker rate limits (requests per second)
    BROKER_LIMITS = {
        "Robinhood": 5,  # Conservative limit
        "Tradier": 2,  # 120 per minute = 2 per second
        "TastyTrade": 10,  # Reasonable default
        "Public": 20,  # Higher limit
        "Firstrade": 5,  # Conservative
        "Fennel": 10,  # Reasonable default
        "Schwab": 5,  # Conservative
        "BBAE": 5,  # Conservative
        "DSPAC": 5,  # Conservative
        "SoFi": 5,  # Conservative
        "Webull": 1,  # Official OpenAPI order limit is 1/s
        "WellsFargo": 5,  # Conservative
        "Chase": 5,  # Conservative (browser automation)
    }

    def __init__(self):
        self.last_call_time = {}
        self._lock = threading.Lock()

    async def wait_if_needed(self, broker_name: str):
        """Wait if necessary to respect per-broker rate limits."""
        # Get broker-specific limit or use default of 10 req/sec
        calls_per_second = self.BROKER_LIMITS.get(broker_name, 10)
        min_interval = 1.0 / calls_per_second

        with self._lock:
            current_time = time.time()
            last_call = self.last_call_time.get(broker_name, 0)
            time_since_last = current_time - last_call

            if time_since_last < min_interval:
                wait_time = min_interval - time_since_last
            else:
                wait_time = 0

            # Update the last call time before releasing lock
            self.last_call_time[broker_name] = current_time + wait_time

        if wait_time > 0:
            await asyncio.sleep(wait_time)


# Global rate limiter
rate_limiter = RateLimiter()


def broker_event(
    message: str,
    *,
    level: str = "info",
    logger: logging.Logger | None = None,
    exc: BaseException | None = None,
) -> None:
    """Emit broker messages to both logs and stdout."""
    active_logger = logger or logging.getLogger(__name__)
    log_fn = getattr(active_logger, level, active_logger.info)
    if exc is not None:
        log_fn(message, exc_info=exc)
    else:
        log_fn(message)
    print(message)


class APICache:
    """Simple in-memory LRU cache for API responses with TTL expiry.

    Backed by an OrderedDict so eviction is O(1) (pop the oldest entry)
    instead of O(n) (scanning every timestamp for the minimum). Reads move
    the key to the most-recently-used end, making eviction true LRU.

    Thread-safe for Free-threaded Python 3.14 (no-GIL).
    """

    def __init__(self, max_size=1000, ttl=300):  # 5 minutes TTL
        self.max_size = max_size
        self.ttl = ttl
        # key -> (value, timestamp); ordered oldest-used -> newest-used
        self._cache: "OrderedDict[str, tuple[Any, float]]" = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """Get cached value if not expired."""
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            value, timestamp = entry
            if time.time() - timestamp < self.ttl:
                self._cache.move_to_end(key)  # mark as most-recently-used
                return value
            # Expired, remove
            del self._cache[key]
            return None

    def set(self, key: str, value: Any):
        """Set cached value with timestamp, evicting the LRU entry if full."""
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, time.time())
            if len(self._cache) > self.max_size:
                self._cache.popitem(last=False)  # evict least-recently-used

    def clear(self):
        """Clear all cached data."""
        with self._lock:
            self._cache.clear()


# Global API cache
api_cache = APICache()


class BrokerConfig:
    """Centralized broker configuration, derived from the broker registry.

    ``BROKERS`` is no longer hand-maintained here — it is projected from
    ``brokers.registry`` (the single source of truth; see ADR 0004). This view
    preserves the historical dict shape so existing callers
    (``get_broker_info`` / ``get_session_key`` / membership checks) are
    unchanged. To add or change a broker, edit ``brokers/registry.py``.
    """

    BROKERS: ClassVar[Dict[str, Dict[str, Any]]] = {
        spec.name: {
            "session_key": spec.session_key,
            "env_vars": list(spec.env_vars),
            "requires_mfa": spec.requires_mfa,
            "enabled": spec.enabled,
        }
        for spec in _REGISTRY.values()
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
                await asyncio.sleep(delay * (2**attempt))  # Exponential backoff
            continue

    if last_exception:
        raise last_exception
    else:
        raise Exception("Retry operation failed")


async def _login_broker(broker_api, broker_name):
    """Helper function to handle login flow for BBAE and DSPAC brokers"""
    try:
        await asyncio.to_thread(broker_api.make_initial_request)
        login_ticket = await asyncio.to_thread(broker_api.generate_login_ticket_email)

        if login_ticket.get("Data") is None:
            raise Exception("Invalid response from generating login ticket")

        if login_ticket.get("Data").get("needSmsVerifyCode", False):
            from tui.input_handler import tui_async_input
            if login_ticket.get("Data").get("needCaptchaCode", False):
                captcha_image = await asyncio.to_thread(broker_api.request_captcha)
                captcha_path = PROJECT_ROOT / f"{broker_name}captcha.png"
                await asyncio.to_thread(captcha_image.save, captcha_path, format="PNG")
                captcha_input = await tui_async_input(
                    f"CAPTCHA image saved to {captcha_path}. Please open it and type in the code: "
                )
                await asyncio.to_thread(
                    broker_api.request_email_code, captcha_input=captcha_input
                )
            else:
                await asyncio.to_thread(broker_api.request_email_code)

            otp_code = await tui_async_input(f"Enter {broker_name} security code: ")
            login_ticket = await asyncio.to_thread(
                broker_api.generate_login_ticket_email, otp_code
            )

        login_response = await asyncio.to_thread(
            broker_api.login_with_ticket, login_ticket.get("Data").get("ticket")
        )
        if login_response.get("Outcome") != "Success":
            raise Exception(f"Login failed. Response: {login_response}")

        return True

    except Exception as e:
        if (
            isinstance(e, CliRuntimeError)
            and e.exit_code == ExitCode.NON_INTERACTIVE_INPUT_REQUIRED
        ):
            raise
        print(f"Error logging into {broker_name}: {e}")
        return False


async def _get_broker_holdings(broker_api, broker_name, ticker=None):
    """Helper function to get holdings for BBAE and DSPAC brokers"""
    try:
        holdings_data = {}
        holdings_response = await asyncio.to_thread(broker_api.get_account_holdings)

        if holdings_response.get("Outcome") != "Success":
            raise Exception(
                f"Failed to get holdings: {holdings_response.get('Message')}"
            )

        positions = holdings_response.get("Data", [])

        if ticker:
            positions = [pos for pos in positions if pos.get("Symbol") == ticker]

        account_info = await asyncio.to_thread(broker_api.get_account_info)
        account_number = account_info.get("Data").get("accountNumber")

        formatted_positions = [
            {
                "symbol": pos.get("Symbol", "Unknown"),
                "quantity": float(pos.get("CurrentAmount", 0)),
                "cost_basis": float(pos.get("CostPrice", 0)),
                "current_value": float(pos.get("Last", 0))
                * float(pos.get("CurrentAmount", 0)),
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
