"""
StockShotGun broker integrations.

This package contains modular broker implementations for multi-broker trading.
Each broker has its own module with trade and holdings functions.

The package no longer eagerly imports the individual broker modules. Broker
identity and function bindings live in ``brokers.registry`` (the single source
of truth; ADR 0004) and are resolved lazily via ``importlib``. That keeps
``import brokers`` — and a single-broker process — from pulling in all thirteen
broker SDKs. Resolve a broker's functions through ``brokers.registry`` (e.g.
``registry.resolve_trade("Robinhood")`` or ``registry.broker_functions_map()``).
"""

# Import base infrastructure
from brokers.base import (
    http_client,
    rate_limiter,
    api_cache,
    BrokerConfig,
    RateLimiter,
    APICache,
    RetryableError,
    retry_operation,
    RETRY_ATTEMPTS,
    RETRY_DELAY,
    RATE_LIMIT_DELAY,
    RATE_LIMIT_WINDOW,
)

# Import session manager
from brokers.session_manager import BrokerSessionManager, session_manager

# The single source of truth for broker identity + function bindings.
from brokers import registry

__all__ = [
    # Base infrastructure
    "http_client",
    "rate_limiter",
    "api_cache",
    "BrokerConfig",
    "RateLimiter",
    "APICache",
    "RetryableError",
    "retry_operation",
    "RETRY_ATTEMPTS",
    "RETRY_DELAY",
    "RATE_LIMIT_DELAY",
    "RATE_LIMIT_WINDOW",
    # Session manager
    "BrokerSessionManager",
    "session_manager",
    # Registry (single source of truth)
    "registry",
]
