"""Subprocess broker MCP smoke test.

Spawns one real broker MCP child (fennel — simplest, no credentials needed
for module-load and health_check) and verifies the proxy can drive it end-to-
end over MCP stdio: `health_check` + `list_accounts_at_broker` round-trip
through the subprocess and return the expected shapes.

Skipped if the `mcp` SDK is not installed (the proxy lazy-imports it). Live
order placement against a subprocess is NOT exercised here — it would require
the broker SDK's credentials in CI; the in-process tests already cover the
per-leg-token validation path that subprocess uses identically.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

mcp = pytest.importorskip("mcp")

from agentic._subprocess import SubprocessBrokerProxy


def _run(coro):
    return asyncio.run(coro)


def test_subprocess_proxy_health_check():
    """`SubprocessBrokerProxy` spawns the broker MCP and returns its health dict."""

    async def _check():
        proxy = SubprocessBrokerProxy(
            name="Fennel",
            module="agentic.brokers.fennel",
            extra_env={"PYTHONPATH": "src", **os.environ},
        )
        async with proxy:
            health = await proxy.health_check()
            assert health["broker"] == "Fennel"
            assert health["ok"] is True
            accounts = await proxy.list_accounts_at_broker()
            assert accounts == ["primary"]

    _run(_check())
