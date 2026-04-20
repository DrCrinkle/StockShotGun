"""Session manager for broker authentication and session handling.

Free-threaded Python 3.14 Compatibility:
- All shared state is protected by threading.Lock for thread-safety
- Asyncio primitives remain for async coordination
"""

import os
import asyncio
import logging
import threading
from .base import BrokerConfig
from cli_runtime import CliRuntimeError, ExitCode  # type: ignore[import-untyped]

# Import broker modules dynamically
from . import robinhood
from . import tradier
from . import tastytrade
from . import public
from . import firstrade
from . import fennel
from . import schwab
from . import bbae
from . import dspac
from . import sofi
from . import webull
from . import wellsfargo
from . import chase

logger = logging.getLogger(__name__)


class BrokerSessionManager:
    """Manages authentication sessions for all brokers to avoid repeated logins."""

    SELF_LOCKING_BROKERS = frozenset({"Robinhood"})

    # Mapping of broker names to their modules and session getter functions
    BROKER_MODULES = {
        "Robinhood": (robinhood, "get_robinhood_session"),
        "Tradier": (tradier, "get_tradier_session"),
        "TastyTrade": (tastytrade, "get_tastytrade_session"),
        "Public": (public, "get_public_session"),
        "Firstrade": (firstrade, "get_firstrade_session"),
        "Fennel": (fennel, "get_fennel_session"),
        "Schwab": (schwab, "get_schwab_session"),
        "BBAE": (bbae, "get_bbae_session"),
        "DSPAC": (dspac, "get_dspac_session"),
        "SoFi": (sofi, "get_sofi_session"),
        "Webull": (webull, "get_webull_session"),
        "WellsFargo": (wellsfargo, "get_wellsfargo_session"),
        "Chase": (chase, "get_chase_session"),
    }

    def __init__(self):
        self.sessions = {}
        self._initialized = set()
        self._env_cache = {}  # Cache environment variables
        self._session_locks = {}  # Prevent concurrent session creation for same broker
        self._lock = threading.Lock()  # Thread-safe access to shared state

    def _get_env(self, key: str, default: str | None = None) -> str:
        """Get environment variable with caching."""
        with self._lock:
            if key not in self._env_cache:
                self._env_cache[key] = os.getenv(key, default)
            return self._env_cache[key]

    def _get_session_lock(self, broker_name: str) -> asyncio.Lock:
        """Get or create a lock for a specific broker to prevent concurrent initialization."""
        with self._lock:
            if broker_name not in self._session_locks:
                self._session_locks[broker_name] = asyncio.Lock()
            return self._session_locks[broker_name]

    async def get_session(self, broker_name: str):
        """
        Universal session getter for any broker.

        Args:
            broker_name: Display name of the broker (e.g., "Robinhood", "TastyTrade")

        Returns:
            Session object for the broker, or None if broker not found
        """
        if broker_name not in self.BROKER_MODULES:
            logger.warning("Unknown broker requested", extra={"broker": broker_name})
            print(f"⚠️  Unknown broker: {broker_name}")
            return None

        # Get module and function name from mapping
        module, func_name = self.BROKER_MODULES[broker_name]

        # Get the session getter function from the module
        session_getter = getattr(module, func_name, None)
        if not session_getter:
            logger.error(
                "Session getter not found",
                extra={"broker": broker_name, "getter": func_name},
            )
            print(f"⚠️  Session getter not found for {broker_name}")
            return None

        # Most broker getters mutate shared state without taking the session lock.
        # Centralize that locking here to prevent duplicate initialization flows.
        if broker_name in self.SELF_LOCKING_BROKERS:
            return await session_getter(self)

        async with self._get_session_lock(broker_name):
            return await session_getter(self)

    async def initialize_selected_sessions(self, broker_names):
        """Initialize sessions only for the specified brokers."""
        if not broker_names:
            print("No brokers selected for initialization")
            return

        print(
            f"🔐 Initializing sessions for selected brokers: {', '.join(broker_names)}"
        )

        # Initialize only selected brokers using dynamic session getter
        tasks = []
        for broker_name in broker_names:
            if broker_name in self.BROKER_MODULES:
                tasks.append(self.get_session(broker_name))
            else:
                print(f"⚠️  Unknown broker: {broker_name}")

        if tasks:
            init_results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in init_results:
                if (
                    isinstance(result, CliRuntimeError)
                    and result.exit_code == ExitCode.NON_INTERACTIVE_INPUT_REQUIRED
                ):
                    raise result

        # Report which sessions are now active
        active_sessions = []
        for broker_name in broker_names:
            session_key = BrokerConfig.get_session_key(broker_name)
            with self._lock:
                has_session = (
                    session_key
                    and session_key in self.sessions
                    and self.sessions[session_key] is not None
                )
            if has_session:
                active_sessions.append(broker_name)

        if active_sessions:
            print(
                f"✅ Selected broker sessions initialized: {', '.join(active_sessions)}"
            )
        else:
            print("⚠️  No broker sessions were successfully initialized")

    async def initialize_all_sessions(self):
        """Initialize all broker sessions concurrently."""
        print("🔐 Initializing broker sessions...")

        # Initialize all brokers dynamically using the mapping
        all_broker_names = list(self.BROKER_MODULES.keys())
        tasks = [self.get_session(broker_name) for broker_name in all_broker_names]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        with self._lock:
            active_sessions = [
                name for name, session in self.sessions.items() if session is not None
            ]
        print(
            f"✅ Session initialization complete. Active sessions: {', '.join(active_sessions)}"
        )

    def cleanup(self):
        """Clean up broker sessions (safe to call multiple times)."""
        # Clear session references - http_client stays open for reuse
        with self._lock:
            self.sessions.clear()
            self._initialized.clear()

    async def shutdown(self):
        """Shutdown browser clients and close HTTP client (call only on application exit)."""
        # Close any browser-based broker clients
        with self._lock:
            session_items = list(self.sessions.items())

        for session_key, session in session_items:
            if not isinstance(session, dict):
                continue
            client = session.get("client")
            if client and hasattr(client, "__aexit__"):
                try:
                    await client.__aexit__(None, None, None)
                    logger.info("Closed browser client", extra={"broker": session_key})
                except Exception as e:
                    logger.warning(
                        "Error closing browser client",
                        extra={"broker": session_key},
                        exc_info=e,
                    )

        from .base import http_client

        try:
            await http_client.aclose()
        except Exception as e:
            logger.warning("Error closing shared HTTP client", exc_info=e)
            print(f"Warning: Error closing HTTP client: {e}")

        # Also clear sessions
        self.cleanup()


# Global session manager instance
session_manager = BrokerSessionManager()
