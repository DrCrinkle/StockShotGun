"""Generic per-broker MCP entrypoint: ``python -m agentic.broker <BrokerName>``.

Resolves ONE broker from the registry and runs its MCP server over stdio.
Because the registry is lazy, this imports only the named broker's module — true
per-broker process isolation. This supersedes ADR 0003's per-broker
``python -m agentic.brokers.<name>`` module paths (see ADR 0004).

Broker name matching is case-insensitive for convenience (``fennel`` resolves
``Fennel``).
"""

from __future__ import annotations

import sys

from agentic._base import build_broker_mcp_spec, run_stdio
from brokers import registry


def _resolve_spec(name: str) -> "registry.BrokerSpec | None":
    spec = registry.get(name)
    if spec is not None:
        return spec
    match = next((n for n in registry.all_names() if n.lower() == name.lower()), None)
    return registry.get(match) if match else None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    known = ", ".join(registry.all_names())
    if not argv:
        print("usage: python -m agentic.broker <BrokerName>", file=sys.stderr)
        print(f"known brokers: {known}", file=sys.stderr)
        return 2

    spec = _resolve_spec(argv[0])
    if spec is None:
        print(f"unknown broker: {argv[0]!r}", file=sys.stderr)
        print(f"known brokers: {known}", file=sys.stderr)
        return 2

    run_stdio(build_broker_mcp_spec(spec))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
