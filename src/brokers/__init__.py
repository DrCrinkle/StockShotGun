"""
StockShotGun broker integrations.

This package contains modular broker implementations for multi-broker trading.
Each broker has its own module with trade and holdings functions.
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

# Import individual broker functions
from brokers.robinhood import robinTrade, robinGetHoldings
from brokers.tradier import tradierTrade, tradierGetHoldings
from brokers.tastytrade import tastyTrade, tastyGetHoldings
from brokers.public import publicTrade, publicGetHoldings
from brokers.firstrade import firstradeTrade, firstradeGetHoldings
from brokers.fennel import fennelTrade, fennelGetHoldings
from brokers.schwab import schwabTrade, schwabGetHoldings
from brokers.bbae import bbaeTrade, bbaeGetHoldings
from brokers.dspac import dspacTrade, dspacGetHoldings
from brokers.sofi import sofiTrade, sofiGetHoldings
from brokers.webull import webullTrade, webullGetHoldings
from brokers.wellsfargo import wellsfargoTrade, wellsfargoGetHoldings

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
    # Webull
    "webullTrade",
    "webullGetHoldings",
    # WellsFargo
    "wellsfargoTrade",
    "wellsfargoGetHoldings",
]
