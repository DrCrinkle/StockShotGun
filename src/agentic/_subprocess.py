"""Subprocess broker MCP client — talks to a per-broker MCP child process over stdio.

`SubprocessBrokerProxy` exposes the same async interface as `BrokerMCPServer`
(`health_check`, `get_holdings_at_broker`, `list_accounts_at_broker`,
`place_at_broker`) but routes each call as an MCP `call_tool` over a stdio
subprocess. Each broker's MCP runs as `python -m agentic.broker <BrokerName>`
in its own process — crashes, leaks, and credentials are isolated. Because the
registry is lazy, that child imports only the named broker (ADR 0004).

The trust model is identical to in-process: the broker validates each leg
token against its own single-target intent at `place_at_broker`. The router
hands out per-leg tokens from the same `EnforcementCore.propose_order` call;
the broker subprocess sees only the leg token + leg parameters.

Lifecycle: `connect()` spawns the child and initializes the MCP session;
`close()` shuts it down. Use as an async context manager when possible.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Any

from agentic._base import PlaceResult


@dataclass
class SubprocessBrokerProxy:
    """Async proxy to a per-broker MCP subprocess.

    Construct with the broker's display name (e.g. "Fennel"). The child is
    spawned as `python -m <module> <name>` (default module `agentic.broker`,
    the generic per-broker entrypoint). Call `connect()` before invoking any
    tool method. The proxy maintains one persistent ClientSession over the
    spawned child's stdio for the proxy's lifetime — calls are multiplexed
    over the same session.
    """

    name: str
    module: str = "agentic.broker"
    python_executable: str = sys.executable
    extra_env: dict[str, str] | None = None
    _stack: AsyncExitStack | None = None
    _session: Any = None  # mcp.ClientSession

    async def connect(self) -> None:
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        env = dict(os.environ)
        if self.extra_env:
            env.update(self.extra_env)
        params = StdioServerParameters(
            command=self.python_executable,
            args=["-m", self.module, self.name],
            env=env,
        )
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
        except Exception:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
            self._session = None

    async def __aenter__(self) -> "SubprocessBrokerProxy":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _require_session(self) -> Any:
        if self._session is None:
            raise RuntimeError(
                f"SubprocessBrokerProxy({self.name}) not connected — call connect() first"
            )
        return self._session

    async def _call(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Invoke `tool_name` over MCP and return the parsed result dict.

        FastMCP wraps return values as TextContent blocks holding JSON; we
        find the first decodable JSON dict in the response.
        """
        session = self._require_session()
        result = await session.call_tool(tool_name, args)
        return _extract_dict(result)

    async def health_check(self) -> dict[str, Any]:
        return await self._call("health_check", {})

    async def get_holdings_at_broker(self, ticker: str | None = None) -> Any:
        result = await self._call(
            "get_holdings_at_broker", {"ticker": ticker} if ticker else {}
        )
        return result.get("holdings", result)

    async def list_accounts_at_broker(self) -> list[str]:
        result = await self._call("list_accounts_at_broker", {})
        accounts = result.get("account_ids", [])
        return list(accounts) if accounts else ["primary"]

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
    ) -> PlaceResult:
        args: dict[str, Any] = {
            "ticker": ticker,
            "qty": qty,
            "side": side,
            "account_id": account_id,
            "dry_run": dry_run,
            "confirmation_token": confirmation_token,
        }
        if price is not None:
            args["price"] = price
        payload = await self._call("place_at_broker", args)
        return PlaceResult(
            ok=bool(payload.get("ok", False)),
            broker=str(payload.get("broker", self.name)),
            account_id=str(payload.get("account_id", account_id)),
            idempotency_key=str(payload.get("idempotency_key", "")),
            dry_run=bool(payload.get("dry_run", dry_run)),
            reason=payload.get("reason"),
            detail=payload.get("detail"),
            fill_qty=payload.get("fill_qty"),
            fill_price=payload.get("fill_price"),
        )


def _extract_dict(call_tool_result: Any) -> dict[str, Any]:
    """FastMCP `call_tool` returns either a tuple of content blocks or a
    direct dict, depending on SDK version. Find the dict payload."""
    candidates = call_tool_result if isinstance(call_tool_result, tuple) else (call_tool_result,)
    for item in candidates:
        if isinstance(item, dict):
            return item
        if isinstance(item, list):
            for block in item:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    try:
                        decoded = json.loads(text)
                        if isinstance(decoded, dict):
                            return decoded
                    except json.JSONDecodeError:
                        continue
        # Some SDK versions return objects with .content list directly
        content = getattr(item, "content", None)
        if isinstance(content, list):
            for block in content:
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    try:
                        decoded = json.loads(text)
                        if isinstance(decoded, dict):
                            return decoded
                    except json.JSONDecodeError:
                        continue
        # Or a structuredContent field on a CallToolResult
        structured = getattr(item, "structuredContent", None)
        if isinstance(structured, dict):
            return structured
    return {}
