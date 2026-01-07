"""Utility functions for getting broker trade and holdings functions."""

from brokers import (
    BrokerConfig,
    robinTrade,
    tradierTrade,
    tastyTrade,
    publicTrade,
    firstradeTrade,
    fennelTrade,
    schwabTrade,
    bbaeTrade,
    dspacTrade,
    sofiTrade,
    webullTrade,
    wellsfargoTrade,
    tradierGetHoldings,
    bbaeGetHoldings,
    dspacGetHoldings,
    publicGetHoldings,
    tastyGetHoldings,
    robinGetHoldings,
    schwabGetHoldings,
    fennelGetHoldings,
    firstradeGetHoldings,
    sofiGetHoldings,
    webullGetHoldings,
    wellsfargoGetHoldings,
)

# Broker function mapping (references to actual trade/holdings functions)
# Enabled status is managed centrally in BrokerConfig (brokers/base.py)
BROKER_CONFIG = {
    "Robinhood": {
        "trade": robinTrade,
        "holdings": robinGetHoldings,
    },
    "Tradier": {
        "trade": tradierTrade,
        "holdings": tradierGetHoldings,
    },
    "TastyTrade": {
        "trade": tastyTrade,
        "holdings": tastyGetHoldings,
    },
    "Public": {
        "trade": publicTrade,
        "holdings": publicGetHoldings,
    },
    "Firstrade": {
        "trade": firstradeTrade,
        "holdings": firstradeGetHoldings,
    },
    "Fennel": {
        "trade": fennelTrade,
        "holdings": fennelGetHoldings,
    },
    "Schwab": {
        "trade": schwabTrade,
        "holdings": schwabGetHoldings,
    },
    "BBAE": {
        "trade": bbaeTrade,
        "holdings": bbaeGetHoldings,
    },
    "DSPAC": {
        "trade": dspacTrade,
        "holdings": dspacGetHoldings,
    },
    "SoFi": {
        "trade": sofiTrade,
        "holdings": sofiGetHoldings,
    },
    "Webull": {
        "trade": webullTrade,
        "holdings": webullGetHoldings,
    },
    "WellsFargo": {
        "trade": wellsfargoTrade,
        "holdings": wellsfargoGetHoldings,
    },
}


def get_broker_function(broker_name, function_type):
    """Get a broker's trade or holdings function if the broker is enabled."""
    # Check if broker exists in function mapping
    if broker_name not in BROKER_CONFIG:
        return None

    # Check if broker is enabled using centralized configuration
    broker_info = BrokerConfig.get_broker_info(broker_name)
    if not broker_info or not broker_info.get("enabled", False):
        return None

    return BROKER_CONFIG[broker_name].get(function_type)
