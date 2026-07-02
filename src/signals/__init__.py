"""Signal source adapters for RSA candidate detection."""

from signals.nasdaq import (
    SOURCE_NAME,
    CalendarSignal,
    fetch_splits_calendar,
    parse_splits_payload,
)

__all__ = [
    "SOURCE_NAME",
    "CalendarSignal",
    "fetch_splits_calendar",
    "parse_splits_payload",
]
