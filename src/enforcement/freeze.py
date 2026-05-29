from __future__ import annotations

import os

from enforcement.errors import FrozenTicker


def frozen_tickers() -> frozenset[str]:
    """Comma-separated UPPER-CASED tickers in `SSG_FROZEN_TICKERS`.

    Evaluated at gate time, not at import — so an operator can update the env
    var via a wrapper script without restarting long-running MCP processes.
    """
    raw = os.getenv("SSG_FROZEN_TICKERS", "")
    return frozenset(
        t.strip().upper() for t in raw.split(",") if t.strip()
    )


def check_freeze(ticker: str) -> None:
    if ticker.upper().strip() in frozen_tickers():
        raise FrozenTicker(
            f"ticker {ticker.upper()} is on the corporate-action freeze list "
            f"(SSG_FROZEN_TICKERS); orders rejected until removed"
        )
