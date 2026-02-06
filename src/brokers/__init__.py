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
from brokers.robinhood import robinTrade, robinGetHoldings, robinValidate
from brokers.tradier import tradierTrade, tradierGetHoldings, tradierValidate
from brokers.tastytrade import tastyTrade, tastyGetHoldings, tastyValidate
from brokers.public import publicTrade, publicGetHoldings
from brokers.firstrade import firstradeTrade, firstradeGetHoldings, firstradeValidate
from brokers.fennel import fennelTrade, fennelGetHoldings
from brokers.schwab import schwabTrade, schwabGetHoldings, schwabValidate
from brokers.bbae import bbaeTrade, bbaeGetHoldings, bbaeValidate
from brokers.dspac import dspacTrade, dspacGetHoldings, dspacValidate
from brokers.sofi import sofiTrade, sofiGetHoldings, sofiValidate
from brokers.webull import webullTrade, webullGetHoldings, webullValidate
from brokers.wellsfargo import wellsfargoTrade, wellsfargoGetHoldings
from brokers.chase import chaseTrade, chaseGetHoldings

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
    "robinValidate",
    # Tradier
    "tradierTrade",
    "tradierGetHoldings",
    "tradierValidate",
    # TastyTrade
    "tastyTrade",
    "tastyGetHoldings",
    "tastyValidate",
    # Public
    "publicTrade",
    "publicGetHoldings",
    # Firstrade
    "firstradeTrade",
    "firstradeGetHoldings",
    "firstradeValidate",
    # Fennel
    "fennelTrade",
    "fennelGetHoldings",
    # Schwab
    "schwabTrade",
    "schwabGetHoldings",
    "schwabValidate",
    # BBAE
    "bbaeTrade",
    "bbaeGetHoldings",
    "bbaeValidate",
    # DSPAC
    "dspacTrade",
    "dspacGetHoldings",
    "dspacValidate",
    # SoFi
    "sofiTrade",
    "sofiGetHoldings",
    "sofiValidate",
    # Webull
    "webullTrade",
    "webullGetHoldings",
    "webullValidate",
    # WellsFargo
    "wellsfargoTrade",
    "wellsfargoGetHoldings",
    # Chase
    "chaseTrade",
    "chaseGetHoldings",
]
