"""BrokerPort — the broker contract the engine depends on (ADR 0006 step 2).

Both `InProcessBroker` (execution/in_process.py) and `SubprocessBrokerProxy`
(agentic/_subprocess.py) satisfy this protocol structurally. The engine
annotates its broker map as `dict[str, BrokerPort]`, so it depends on the
contract — not on whether a broker runs in-process or as an isolated
subprocess. That decoupling is what lets the in-process / subprocess choice
(ADR 0003) be pure wiring.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from execution.in_process import PlaceResult


@runtime_checkable
class BrokerPort(Protocol):
    """The four canonical per-broker operations the engine fans out across."""

    async def health_check(self) -> dict[str, Any]: ...

    async def get_holdings_at_broker(self, ticker: str | None = None) -> Any: ...

    async def list_accounts_at_broker(self) -> list[str]: ...

    async def place_at_broker(
        self,
        *,
        ticker: str,
        qty: float,
        side: str,
        price: float | None,
        account_id: str,
        dry_run: bool,
        confirmation_token: str,
    ) -> "PlaceResult": ...
