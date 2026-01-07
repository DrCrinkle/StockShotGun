"""Session status caching for the TUI."""

import time
from brokers import session_manager


class SessionStatusCache:
    """Cache for broker session status to avoid repeated lookups."""

    def __init__(self):
        self._cache = {}
        self._cache_timeout = 5.0  # Cache timeout in seconds
        self._last_update = 0

    def get_session_status(self, broker_name):
        """Get cached session status for a broker."""
        current_time = time.time()

        # Refresh cache if expired
        if current_time - self._last_update > self._cache_timeout:
            self._refresh_cache()
            self._last_update = current_time

        return self._cache.get(broker_name, False)

    def _refresh_cache(self):
        """Refresh the session status cache."""
        try:
            sessions = session_manager.sessions
            self._cache = {
                name: session is not None
                for name, session in sessions.items()
            }
        except Exception:
            # If session manager is not available, clear cache
            self._cache = {}


# Global session status cache
session_cache = SessionStatusCache()
