"""Nasdaq corporate-actions splits calendar source adapter.

Pure source layer: fetch + parse only. Storage lives in
`AutomationRecapStore.upsert_calendar_signals`; orchestration in the
`signals` CLI and the router's `scan_signals`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

NASDAQ_SPLITS_URL = "https://api.nasdaq.com/api/calendar/splits"

# Nasdaq's API returns empty/hangs without browser-like headers.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# Real Nasdaq data only ever uses ":" as the separator (e.g. "1 : 20").
# "x" and "/" are accepted defensively against upstream formatting drift,
# not formats observed in production payloads.
_RATIO_RE = re.compile(r"^\s*(\d+)\s*[:x/]\s*(\d+)\s*$")

SOURCE_NAME = "nasdaq_calendar"


@dataclass(frozen=True)
class CalendarSignal:
    ticker: str
    ratio: str  # normalized "N:D", reverse splits only (N < D)
    effective_date: str | None  # ISO YYYY-MM-DD, None if unparseable
    company: str
    raw: dict[str, Any] = field(compare=False)


def _parse_ratio(raw_ratio: Any) -> tuple[int, int] | None:
    """Parse a Nasdaq ratio string into (numerator, denominator).

    Only accepts pure integer ratios ("1 : 10", "3:1"). Decimal ratios
    ("1.5:1") and percentage-style rows ("5%") are real upstream shapes
    Nasdaq emits for certain corporate actions (fund distributions, etc.)
    that aren't ordinary splits — treated as unparseable, not fatal.
    """
    if not isinstance(raw_ratio, str):
        return None
    m = _RATIO_RE.match(raw_ratio)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _parse_date(raw_date: Any) -> str | None:
    if not isinstance(raw_date, str) or not raw_date.strip():
        return None
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw_date.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_splits_payload(payload: dict[str, Any] | None) -> list[CalendarSignal]:
    """Extract reverse splits from a Nasdaq splits-calendar payload.

    Malformed rows are skipped, never fatal — the calendar is upstream
    data we don't control.
    """
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    signals: list[CalendarSignal] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ticker = row.get("symbol")
        if not isinstance(ticker, str) or not ticker.strip():
            continue
        parsed = _parse_ratio(row.get("ratio"))
        if parsed is None:
            continue
        num, den = parsed
        if num <= 0 or den <= 0 or num >= den:  # forward split, 1:1, or zero-numerator — not an RSA candidate
            continue
        signals.append(
            CalendarSignal(
                ticker=ticker.strip().upper(),
                ratio=f"{num}:{den}",
                effective_date=_parse_date(row.get("executionDate")),
                company=str(row.get("name") or ""),
                raw=row,
            )
        )
    return signals


async def fetch_splits_calendar(date_str: str | None = None) -> dict[str, Any]:
    """Fetch the raw splits-calendar payload for a date (defaults to today
    on the Nasdaq side). Network errors propagate to the caller."""
    params = {"date": date_str} if date_str else {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(NASDAQ_SPLITS_URL, params=params, headers=_HEADERS)
        response.raise_for_status()
        return response.json()
