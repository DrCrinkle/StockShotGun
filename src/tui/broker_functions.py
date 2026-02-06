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
    chaseTrade,
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
    chaseGetHoldings,
    robinValidate,
    tradierValidate,
    tastyValidate,
    firstradeValidate,
    schwabValidate,
    bbaeValidate,
    dspacValidate,
    sofiValidate,
    webullValidate,
)

# Broker function mapping (references to actual trade/holdings functions)
# Enabled status is managed centrally in BrokerConfig (brokers/base.py)
BROKER_CONFIG = {
    "Robinhood": {
        "trade": robinTrade,
        "holdings": robinGetHoldings,
        "validate": robinValidate,
    },
    "Tradier": {
        "trade": tradierTrade,
        "holdings": tradierGetHoldings,
        "validate": tradierValidate,
    },
    "TastyTrade": {
        "trade": tastyTrade,
        "holdings": tastyGetHoldings,
        "validate": tastyValidate,
    },
    "Public": {
        "trade": publicTrade,
        "holdings": publicGetHoldings,
    },
    "Firstrade": {
        "trade": firstradeTrade,
        "holdings": firstradeGetHoldings,
        "validate": firstradeValidate,
    },
    "Fennel": {
        "trade": fennelTrade,
        "holdings": fennelGetHoldings,
    },
    "Schwab": {
        "trade": schwabTrade,
        "holdings": schwabGetHoldings,
        "validate": schwabValidate,
    },
    "BBAE": {
        "trade": bbaeTrade,
        "holdings": bbaeGetHoldings,
        "validate": bbaeValidate,
    },
    "DSPAC": {
        "trade": dspacTrade,
        "holdings": dspacGetHoldings,
        "validate": dspacValidate,
    },
    "SoFi": {
        "trade": sofiTrade,
        "holdings": sofiGetHoldings,
        "validate": sofiValidate,
    },
    "Webull": {
        "trade": webullTrade,
        "holdings": webullGetHoldings,
        "validate": webullValidate,
    },
    "WellsFargo": {
        "trade": wellsfargoTrade,
        "holdings": wellsfargoGetHoldings,
    },
    "Chase": {
        "trade": chaseTrade,
        "holdings": chaseGetHoldings,
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
