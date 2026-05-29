"""DSPAC broker integration (Redbridge Securities).

DSPAC shares its implementation with BBAE via the Redbridge factory; see
``brokers/_redbridge_broker.py``.
"""

from dspac_invest_api import DSPACAPI

from brokers._redbridge_broker import make_redbridge_broker

dspacTrade, dspacValidate, dspacGetHoldings, get_dspac_session = make_redbridge_broker(
    "DSPAC", DSPACAPI
)
