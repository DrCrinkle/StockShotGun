"""Gated CLI — every order-placing code path here routes through `gate_order`.

Operator-facing CLI that uses the in-process `Router` for fan-out, the
`EnforcementCore` for safety gates, and the audit log for tamper-evident
history. Live order placement is a deliberate two-step propose-then-execute
flow; single-call live orders are explicitly forbidden (ISC-18).

Why this exists alongside `main.py`: this is the gated path. The legacy
`main.py` CLI talks directly to broker SDKs via `OrderBatchProcessor` and
bypasses the enforcement core — that path stays available for operators who
need raw access while we migrate, but the gated path is the documented
default for any new tooling.

Subcommands:
  list-brokers                          — per-broker health, MFA, fractional flags
  holdings [TICKER] [--broker B ...]    — fan-out get_holdings across selected brokers
  propose SIDE QTY TICKER [--brokers ...] [--price P] [--dry-run/--live]
                                        — mint a fan-out proposal; prints proposal_id
                                          + per-broker estimated cost; requires
                                          an explicit follow-up `execute` to fire
  execute PROPOSAL_ID [--live]          — execute a previously minted proposal;
                                          dry-run by default — must pass --live
                                          to place real orders
  dry-run SIDE QTY TICKER [--brokers ...] [--price P]
                                        — convenience: propose + dry-execute in one
                                          call; returns the same shape as `execute`
                                          with `dry_run=True`. Never fires live.
  audit-verify                          — walk the tamper-evident audit log; reports
                                          chain integrity status

Live order placement requires the two-step flow:
  $ python -m agentic.cli propose buy 10 TSLA --brokers Fennel,Tradier --price 5.0 --live
  Proposed: <proposal_id>  estimated_usd=100.00  expires=...
  $ python -m agentic.cli execute <proposal_id> --live

All commands honor `--json` for machine-readable output.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from agentic.router import Router

OK = 0
ERR = 1
USAGE = 2


def _print(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, default=str))
        return
    if isinstance(payload, dict):
        for k, v in payload.items():
            print(f"  {k}: {v}")
    elif isinstance(payload, list):
        for item in payload:
            print(item)
    else:
        print(payload)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentic.cli",
        description="Gated multi-broker CLI — every order routes through the enforcement core",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-brokers", help="show per-broker health + capability flags")
    sub.add_parser("audit-verify", help="walk the audit log hash chain; report integrity")

    p_h = sub.add_parser("holdings", help="fan-out get_holdings across brokers")
    p_h.add_argument("ticker", nargs="?", default=None, help="optional ticker filter")
    p_h.add_argument(
        "--broker",
        dest="brokers",
        action="append",
        help="broker name; repeat for multiple; omit for all",
    )

    p_p = sub.add_parser("propose", help="mint a fan-out proposal (does not fire)")
    p_p.add_argument("side", choices=["buy", "sell"])
    p_p.add_argument("qty", type=float)
    p_p.add_argument("ticker")
    p_p.add_argument("--brokers", default=None, help="comma-separated broker names; omit for all")
    p_p.add_argument("--price", type=float, default=None)
    grp = p_p.add_mutually_exclusive_group()
    grp.add_argument("--dry-run", dest="dry_run", action="store_true", default=True)
    grp.add_argument("--live", dest="dry_run", action="store_false")

    p_e = sub.add_parser("execute", help="execute a previously minted proposal")
    p_e.add_argument("proposal_id")
    p_e.add_argument("--live", dest="dry_run", action="store_false", default=True)

    p_d = sub.add_parser(
        "dry-run", help="convenience: propose + dry-execute (never fires live)"
    )
    p_d.add_argument("side", choices=["buy", "sell"])
    p_d.add_argument("qty", type=float)
    p_d.add_argument("ticker")
    p_d.add_argument("--brokers", default=None)
    p_d.add_argument("--price", type=float, default=None)

    return parser


def _parse_broker_list(raw: str | list[str] | None) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, list):
        return raw
    return [b.strip() for b in raw.split(",") if b.strip()]


async def _cmd_list_brokers(router: Router, args: argparse.Namespace) -> int:
    out = await router.list_brokers()
    _print(out, args.json)
    return OK


async def _cmd_holdings(router: Router, args: argparse.Namespace) -> int:
    brokers = _parse_broker_list(args.brokers)
    out = await router.get_holdings(ticker=args.ticker, brokers=brokers)
    _print(out, args.json)
    return OK


async def _cmd_propose(router: Router, args: argparse.Namespace) -> int:
    brokers = _parse_broker_list(args.brokers)
    try:
        out = await router.propose_order(
            ticker=args.ticker,
            qty=args.qty,
            side=args.side,
            brokers=brokers,
            price=args.price,
            dry_run=args.dry_run,
        )
    except Exception as e:  # GateError + any unexpected — surface to operator
        _print(
            {"ok": False, "error": str(e), "reason": getattr(e, "reason", "error")},
            args.json,
        )
        return ERR
    _print(out, args.json)
    return OK


async def _cmd_execute(router: Router, args: argparse.Namespace) -> int:
    out = await router.execute_order(proposal_id=args.proposal_id, dry_run=args.dry_run)
    _print(out, args.json)
    # Successful execution: at least one leg succeeded
    return OK if out.get("success_count", 0) > 0 else ERR


async def _cmd_dry_run(router: Router, args: argparse.Namespace) -> int:
    brokers = _parse_broker_list(args.brokers)
    try:
        proposal = await router.propose_order(
            ticker=args.ticker,
            qty=args.qty,
            side=args.side,
            brokers=brokers,
            price=args.price,
            dry_run=True,
        )
    except Exception as e:
        _print(
            {"ok": False, "error": str(e), "reason": getattr(e, "reason", "error")},
            args.json,
        )
        return ERR
    out = await router.execute_order(proposal_id=proposal["proposal_id"], dry_run=True)
    _print({"proposal": proposal, "execution": out}, args.json)
    return OK


async def _cmd_audit_verify(router: Router, args: argparse.Namespace) -> int:
    ok, lines, msg = router.core.audit.verify()
    _print(
        {
            "ok": ok,
            "lines_checked": lines,
            "first_break": msg,
            "path": str(router.core.audit.path),
        },
        args.json,
    )
    return OK if ok else ERR


HANDLERS = {
    "list-brokers": _cmd_list_brokers,
    "holdings": _cmd_holdings,
    "propose": _cmd_propose,
    "execute": _cmd_execute,
    "dry-run": _cmd_dry_run,
    "audit-verify": _cmd_audit_verify,
}


def run(argv: list[str] | None = None, *, router: Router | None = None) -> int:
    """Entrypoint. `router` lets tests inject a fixture-bound Router; CLI builds
    a default Router (in-process, all 13 brokers, real audit log) when called
    from `python -m agentic.cli`.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    r = router if router is not None else Router.from_all_brokers()
    handler = HANDLERS[args.cmd]
    return asyncio.run(handler(r, args))


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
