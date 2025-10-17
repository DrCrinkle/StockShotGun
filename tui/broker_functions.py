"""Utility functions for getting broker trade and holdings functions."""

from brokers import (
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
)

# Broker configuration mapping
BROKER_CONFIG = {
    "Robinhood": {
        "trade": robinTrade,
        "holdings": robinGetHoldings,
        "enabled": True
    },
    "Tradier": {
        "trade": tradierTrade,
        "holdings": tradierGetHoldings,
        "enabled": True
    },
    "TastyTrade": {
        "trade": tastyTrade,
        "holdings": tastyGetHoldings,
        "enabled": True
    },
    "Public": {
        "trade": publicTrade,
        "holdings": publicGetHoldings,
        "enabled": True
    },
    "Firstrade": {
        "trade": firstradeTrade,
        "holdings": firstradeGetHoldings,
        "enabled": True
    },
    "Fennel": {
        "trade": fennelTrade,
        "holdings": fennelGetHoldings,
        "enabled": True
    },
    "Schwab": {
        "trade": schwabTrade,
        "holdings": schwabGetHoldings,
        "enabled": True
    },
    "BBAE": {
        "trade": bbaeTrade,
        "holdings": bbaeGetHoldings,
        "enabled": True
    },
    "DSPAC": {
        "trade": dspacTrade,
        "holdings": dspacGetHoldings,
        "enabled": True
    },
    "SoFi": {
        "trade": sofiTrade,
        "holdings": sofiGetHoldings,
        "enabled": True
    },
}


def get_broker_function(broker_name, function_type):
    if broker_name not in BROKER_CONFIG:
        return None
    if not BROKER_CONFIG[broker_name]["enabled"]:
        return None
    return BROKER_CONFIG[broker_name].get(function_type)
