"""
StockShotGun broker integrations.

This package contains modular broker implementations for multi-broker trading.
Each broker has its own module with trade and holdings functions.
"""

# Import base infrastructure
from .base import (
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
from .session_manager import BrokerSessionManager, session_manager

# Import individual broker functions
from .robinhood import robinTrade, robinGetHoldings
from .tradier import tradierTrade, tradierGetHoldings
from .tastytrade import tastyTrade, tastyGetHoldings
from .public import publicTrade, publicGetHoldings
from .firstrade import firstradeTrade, firstradeGetHoldings
from .fennel import fennelTrade, fennelGetHoldings
from .schwab import schwabTrade, schwabGetHoldings
from .bbae import bbaeTrade, bbaeGetHoldings
from .dspac import dspacTrade, dspacGetHoldings
from .sofi import sofiTrade, sofiGetHoldings

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
    # Robinhood
    "robinTrade",
    "robinGetHoldings",
    # Tradier
    "tradierTrade",
    "tradierGetHoldings",
    # TastyTrade
    "tastyTrade",
    "tastyGetHoldings",
    # Public
    "publicTrade",
    "publicGetHoldings",
    # Firstrade
    "firstradeTrade",
    "firstradeGetHoldings",
    # Fennel
    "fennelTrade",
    "fennelGetHoldings",
    # Schwab
    "schwabTrade",
    "schwabGetHoldings",
    # BBAE
    "bbaeTrade",
    "bbaeGetHoldings",
    # DSPAC
    "dspacTrade",
    "dspacGetHoldings",
    # SoFi
    "sofiTrade",
    "sofiGetHoldings",
]
