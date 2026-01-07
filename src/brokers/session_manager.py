"""Session manager for broker authentication and session handling."""

import os
import asyncio
from brokers.base import BrokerConfig

# Import broker modules dynamically
import brokers.robinhood
import brokers.tradier
import brokers.tastytrade
import brokers.public
import brokers.firstrade
import brokers.fennel
import brokers.schwab
import brokers.bbae
import brokers.dspac
import brokers.sofi
import brokers.webull
import brokers.wellsfargo


class BrokerSessionManager:
    """Manages authentication sessions for all brokers to avoid repeated logins."""

    # Mapping of broker names to their modules and session getter functions
    BROKER_MODULES = {
        "Robinhood": (brokers.robinhood, "get_robinhood_session"),
        "Tradier": (brokers.tradier, "get_tradier_session"),
        "TastyTrade": (brokers.tastytrade, "get_tastytrade_session"),
        "Public": (brokers.public, "get_public_session"),
        "Firstrade": (brokers.firstrade, "get_firstrade_session"),
        "Fennel": (brokers.fennel, "get_fennel_session"),
        "Schwab": (brokers.schwab, "get_schwab_session"),
        "BBAE": (brokers.bbae, "get_bbae_session"),
        "DSPAC": (brokers.dspac, "get_dspac_session"),
        "SoFi": (brokers.sofi, "get_sofi_session"),
        "Webull": (brokers.webull, "get_webull_session"),
        "WellsFargo": (brokers.wellsfargo, "get_wellsfargo_session"),
    }

    def __init__(self):
        self.sessions = {}
        self._initialized = set()
        self._env_cache = {}  # Cache environment variables
        self._session_locks = {}  # Prevent concurrent session creation for same broker

    def _get_env(self, key: str, default: str | None = None) -> str:
        """Get environment variable with caching."""
        if key not in self._env_cache:
            self._env_cache[key] = os.getenv(key, default)
        return self._env_cache[key]

    def _get_session_lock(self, broker_name: str) -> asyncio.Lock:
        """Get or create a lock for a specific broker to prevent concurrent initialization."""
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
            print(f"⚠️  Unknown broker: {broker_name}")
            return None

        # Get module and function name from mapping
        module, func_name = self.BROKER_MODULES[broker_name]

        # Get the session getter function from the module
        session_getter = getattr(module, func_name, None)
        if not session_getter:
            print(f"⚠️  Session getter not found for {broker_name}")
            return None

        # Call the session getter function with self as parameter
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
            await asyncio.gather(*tasks, return_exceptions=True)

        # Report which sessions are now active
        active_sessions = []
        for broker_name in broker_names:
            session_key = BrokerConfig.get_session_key(broker_name)
            if (
                session_key
                and session_key in self.sessions
                and self.sessions[session_key] is not None
            ):
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

        active_sessions = [
            name for name, session in self.sessions.items() if session is not None
        ]
        print(
            f"✅ Session initialization complete. Active sessions: {', '.join(active_sessions)}"
        )

    def cleanup(self):
        """Clean up broker sessions (safe to call multiple times)."""
        # Clear session references - http_client stays open for reuse
        self.sessions.clear()
        self._initialized.clear()

    async def shutdown(self):
        """Shutdown and close HTTP client (call only on application exit)."""
        from base import http_client

        try:
            await http_client.aclose()
        except Exception as e:
            print(f"Warning: Error closing HTTP client: {e}")

        # Also clear sessions
        self.cleanup()


# Global session manager instance
session_manager = BrokerSessionManager()
