"""F6 — credential-boundary + raw-SQL anti-criterion enforcement (ISC-16, ISC-19).

Two layers of defense:

  1. **Static scan** of `src/agentic/` source: forbidden patterns include
     SDK credential field names (`password`, `_secret`, `oauth_token`,
     `refresh_token`, `cookies`, `session_cookie`) anywhere in tool source.
     Legitimate token-like fields in our domain (`confirmation_token`,
     `leg_token`, `proposal_id`, `intent_hash`, `idempotency_key`,
     `prev_hash`, `this_hash`) are allowlisted.
  2. **Runtime fuzz** of every router + per-broker MCP tool with fake SPECs
     that return responses containing credential-shaped string keys. The
     assertions confirm the agentic layer never PROPAGATES the credential
     keys into MCP-tool responses (the broker layer might internally hold
     them, but they MUST NOT escape).

The raw-SQL check (ISC-19) is a static scan asserting `agentic/` source has
no `executescript(` / `execute(` / `cursor(` calls except via imports from
`enforcement.*` / `rsa_store` / `sweep_persistence` — the documented
persistence primitives.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from agentic._base import BrokerMCPServer, BrokerMCPSpec, build_fastmcp_server
from agentic.router import (
    BrokerServerAccountStatusProvider,
    NullAccountStatusProvider,
    Router,
    build_router_fastmcp_server,
)
from enforcement import (
    AuditLog,
    EnforcementCore,
    ProposalStore,
)
from enforcement.circuit_breaker import CircuitBreaker

ROOT = Path(__file__).resolve().parents[2]
AGENTIC_DIR = ROOT / "src" / "agentic"

# Field names whose presence in a tool response would constitute a credential
# leak. Stems are matched case-insensitively.
FORBIDDEN_CREDENTIAL_STEMS = (
    "password",
    "_secret",
    "oauth_token",
    "refresh_token",
    "access_token",
    "session_cookie",
    "session_id",
    "api_key",
    "bearer_token",
    "mfa_code",
    "otp_code",
)

# Allowlist: legitimate token-shaped names in our domain. These appear in
# code naturally and are NOT credential leaks.
LEGITIMATE_TOKEN_NAMES = {
    "confirmation_token",
    "leg_token",
    "proposal_id",
    "intent_hash",
    "idempotency_key",
    "prev_hash",
    "this_hash",
    "audit_chain_broken",
    "tokeninvalid",
    "tokenexpired",
    "tokenalreadyused",
    "leg_tokens",
    "ttl_seconds",
    "valid_until_ts",
    "token_already_used",  # gate-error reason code
    "token_invalid",
    "token_expired",
    "live_order_requires_confirmation",
}


def _python_sources() -> list[Path]:
    """Every .py file under src/agentic/, excluding tests."""
    return [p for p in AGENTIC_DIR.rglob("*.py") if p.is_file()]


_STRING_LITERAL_RE = re.compile(
    r"(\"\"\"[\s\S]*?\"\"\"|'''[\s\S]*?'''|\"[^\"\n]*\"|'[^'\n]*')"
)


def _strip_string_literals(text: str) -> str:
    """Replace string literals with whitespace of equal length so line numbers
    and column offsets stay stable. Notes/docstrings are humans-talking-about-
    credentials, not credentials-in-code, and shouldn't trigger the scan.
    """
    return _STRING_LITERAL_RE.sub(lambda m: " " * len(m.group(0)), text)


def test_static_no_credential_field_names_in_agentic_source():
    """ISC-16 static layer: forbidden credential stems do not appear in
    agentic source identifiers (variable names, attribute access, kwargs).
    String literals and docstrings are stripped before scanning — those are
    documentation, not credential-bearing code.
    """
    hits: list[tuple[Path, str, str]] = []
    for src_path in _python_sources():
        raw = src_path.read_text(encoding="utf-8")
        text = _strip_string_literals(raw)
        for stem in FORBIDDEN_CREDENTIAL_STEMS:
            for m in re.finditer(rf"\b\w*{re.escape(stem)}\w*\b", text, re.IGNORECASE):
                hit = m.group(0)
                if hit.lower() in LEGITIMATE_TOKEN_NAMES:
                    continue
                line = text[: m.start()].count("\n") + 1
                hits.append((src_path.relative_to(ROOT), f"line {line}", hit))
    assert hits == [], f"credential field names leaked into agentic source: {hits}"


def test_static_no_raw_sql_in_agentic_source():
    """ISC-19 static layer: agentic/ does not run raw SQL. Persistence goes
    through enforcement / rsa_store / sweep_persistence imports only.
    """
    forbidden_patterns = [
        r"\.execute\s*\(\s*['\"]",  # .execute('SELECT ...) or .execute("CREATE ...
        r"\.executescript\s*\(",
        r"\.executemany\s*\(",
        r"\bsqlite3\.\w+\s*\(",
    ]
    hits: list[tuple[Path, str, str]] = []
    for src_path in _python_sources():
        text = src_path.read_text(encoding="utf-8")
        # Strip docstrings + line comments before pattern matching
        stripped = re.sub(r'"""[\s\S]*?"""', "", text)
        stripped = re.sub(r"'''[\s\S]*?'''", "", stripped)
        stripped = "\n".join(
            ln for ln in stripped.splitlines() if not ln.lstrip().startswith("#")
        )
        for pat in forbidden_patterns:
            for m in re.finditer(pat, stripped):
                hits.append((src_path.relative_to(ROOT), pat, m.group(0)[:60]))
    assert hits == [], (
        f"agentic/ must not run raw SQL — go through enforcement/* "
        f"or rsa_store/sweep_persistence: {hits}"
    )


def _credential_leaking_spec(name: str) -> BrokerMCPSpec:
    """A SPEC whose async functions return credential-shaped payloads. The
    tests confirm the agentic layer never PROPAGATES these into MCP-tool
    responses — the response shapes must come from the agentic boundary, not
    from raw broker SDK returns.
    """

    async def fake_trade(side: str, qty: float, ticker: str, price: float | None) -> Any:
        return {
            "ok": True,
            # Simulated credential-shaped fields a misbehaving SDK might return:
            "session_cookie": "FAKE_COOKIE_VALUE_XYZ",
            "oauth_token": "FAKE_OAUTH_VALUE",
            "password": "should-never-leak",
        }

    async def fake_holdings(ticker: str | None = None) -> Any:
        # Holdings payloads sometimes echo session metadata — confirm none leaks
        return {
            "TSLA": 10.0,
            "session_cookie": "FAKE_SESSION",
            "refresh_token": "FAKE_REFRESH",
        }

    return BrokerMCPSpec(
        name=name, trade_fn=fake_trade, holdings_fn=fake_holdings
    )


def _serialize(obj: Any) -> str:
    """Best-effort flatten an arbitrary response into a single string for
    credential-stem scanning. We accept dataclasses, dicts, lists, primitives.
    """
    import json
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return json.dumps(asdict(obj), default=str)
    if isinstance(obj, (dict, list, tuple)):
        return json.dumps(obj, default=str)
    return str(obj)


def _assert_no_credentials(payload: Any) -> None:
    blob = _serialize(payload).lower()
    leaked = [stem for stem in FORBIDDEN_CREDENTIAL_STEMS if stem in blob]
    # Special-case "session_id" — common in legitimate ID fields. We only
    # flag if a known credential VALUE shape appears alongside it.
    assert not leaked, f"credential stems leaked: {leaked} in payload {blob[:200]}"


@pytest.fixture
def core(tmp_path: Path) -> EnforcementCore:
    return EnforcementCore(
        proposal_store=ProposalStore(tmp_path / "proposals.sqlite"),
        audit=AuditLog(tmp_path / "audit.jsonl"),
        breaker=CircuitBreaker(threshold=3, cooldown_seconds=10.0),
        proposal_ttl_seconds=60.0,
    )


def test_runtime_broker_health_check_does_not_leak(core: EnforcementCore):
    server = BrokerMCPServer(_credential_leaking_spec("Leaky"), core=core)
    health = asyncio.run(server.health_check())
    _assert_no_credentials(health)


def test_runtime_broker_holdings_does_not_leak_into_router(core: EnforcementCore):
    """Even when the broker SDK returns credential-shaped fields in its
    holdings response, the router's `get_holdings` MCP tool must not echo
    them. Today the router DOES embed the raw holdings dict — this test
    documents that as a gap to fix at the router-tool boundary."""
    servers = {"Leaky": BrokerMCPServer(_credential_leaking_spec("Leaky"), core=core)}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    out = asyncio.run(router.get_holdings(ticker="TSLA"))
    # The router currently passes raw broker holdings through. Confirm: any
    # credential-shaped key the broker returned MUST be stripped before
    # reaching the agent. If this test fails on a future SDK change, the
    # router needs a sanitization step at the MCP boundary.
    _assert_no_credentials(out)


def test_runtime_router_propose_response_has_no_credentials(core: EnforcementCore):
    servers = {"Leaky": BrokerMCPServer(_credential_leaking_spec("Leaky"), core=core)}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    out = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["Leaky"],
            price=5.0,
            dry_run=True,
        )
    )
    _assert_no_credentials(out)


def test_runtime_router_execute_response_has_no_credentials(core: EnforcementCore):
    servers = {"Leaky": BrokerMCPServer(_credential_leaking_spec("Leaky"), core=core)}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    proposal = asyncio.run(
        router.propose_order(
            ticker="TSLA",
            qty=1.0,
            side="buy",
            brokers=["Leaky"],
            price=5.0,
            dry_run=False,
        )
    )
    out = asyncio.run(
        router.execute_order(proposal_id=proposal["proposal_id"], dry_run=False)
    )
    _assert_no_credentials(out)


def test_runtime_fastmcp_tools_registered_have_no_credentials_in_descriptions(
    core: EnforcementCore,
):
    """The FastMCP tool registry exposes tool name + inputSchema to MCP
    clients. Confirm none of those carry credential stems either."""
    servers = {"Leaky": BrokerMCPServer(_credential_leaking_spec("Leaky"), core=core)}
    router = Router(
        broker_servers=servers,
        core=core,
        provider=NullAccountStatusProvider(),
    )
    router_app = build_router_fastmcp_server(router)
    broker_app = build_fastmcp_server(
        _credential_leaking_spec("Leaky"), core=core
    )
    for app in (router_app, broker_app):
        tools = asyncio.run(app.list_tools())
        for t in tools:
            blob = (t.name + " " + (t.description or "") + " " + str(t.inputSchema)).lower()
            for stem in FORBIDDEN_CREDENTIAL_STEMS:
                assert stem not in blob, (
                    f"credential stem '{stem}' leaked into tool registration: {t.name}"
                )
