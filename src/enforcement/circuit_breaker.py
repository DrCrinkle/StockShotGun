from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from enforcement.errors import CircuitOpen

DEFAULT_CONSECUTIVE_ERROR_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 600.0


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass
class _BrokerState:
    consecutive_errors: int = 0
    opened_at: float | None = None
    last_reason: str | None = None


class CircuitBreaker:
    """Per-broker consecutive-error counter with cooldown.

    State is in-process. Each per-broker MCP owns its own instance; the
    router never sees the underlying state. Globally an additional session
    drawdown gate (ISC-43) is enforced elsewhere — this class only handles
    per-broker error streaks.
    """

    def __init__(
        self,
        threshold: int | None = None,
        cooldown_seconds: float | None = None,
    ):
        self.threshold = threshold or _env_int(
            "SSG_CIRCUIT_THRESHOLD", DEFAULT_CONSECUTIVE_ERROR_THRESHOLD
        )
        self.cooldown = cooldown_seconds or _env_float(
            "SSG_CIRCUIT_COOLDOWN_S", DEFAULT_COOLDOWN_SECONDS
        )
        self._lock = threading.Lock()
        self._states: dict[str, _BrokerState] = {}

    def _state(self, broker: str) -> _BrokerState:
        s = self._states.get(broker)
        if s is None:
            s = _BrokerState()
            self._states[broker] = s
        return s

    def check(self, broker: str) -> None:
        """Raise CircuitOpen if the broker is currently open AND cooldown
        has not elapsed. If cooldown has elapsed, transition to half-open by
        resetting consecutive_errors so the next call is a probe.
        """
        with self._lock:
            s = self._state(broker)
            if s.opened_at is None:
                return
            elapsed = time.time() - s.opened_at
            if elapsed < self.cooldown:
                raise CircuitOpen(
                    f"broker {broker} circuit open ({s.last_reason or 'errors'}); "
                    f"cooldown remaining {self.cooldown - elapsed:.0f}s"
                )
            s.opened_at = None
            s.consecutive_errors = 0
            s.last_reason = None

    def record_success(self, broker: str) -> None:
        with self._lock:
            s = self._state(broker)
            s.consecutive_errors = 0
            s.opened_at = None
            s.last_reason = None

    def record_failure(self, broker: str, reason: str) -> None:
        with self._lock:
            s = self._state(broker)
            s.consecutive_errors += 1
            s.last_reason = reason
            if s.consecutive_errors >= self.threshold and s.opened_at is None:
                s.opened_at = time.time()

    def force_reset(self, broker: str) -> None:
        """Operator-only reset. CLI command surfaces this; agents must not."""
        with self._lock:
            self._states.pop(broker, None)
