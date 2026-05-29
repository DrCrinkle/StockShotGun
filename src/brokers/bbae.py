"""BBAE broker integration (Redbridge Securities).

BBAE shares its implementation with DSPAC via the Redbridge factory; see
``brokers/_redbridge_broker.py``.
"""

from bbae_invest_api import BBAEAPI

from brokers._redbridge_broker import make_redbridge_broker

bbaeTrade, bbaeValidate, bbaeGetHoldings, get_bbae_session = make_redbridge_broker(
    "BBAE", BBAEAPI
)
