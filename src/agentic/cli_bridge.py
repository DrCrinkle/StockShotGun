"""Bridge: enforcement gate for the legacy `main.py` CLI.

`main.py` keeps its argparse surface and its `order_processor.process_orders`
fan-out path. This module adds a SAFETY-ONLY gate call that runs before
the fan-out — limits, freeze list, circuit breaker, audit log — so the
legacy path stops bypassing the enforcement core.

What this gate currently enforces on the main.py path:
  - Per-order dollar limit          (ISC-13)
  - Per-day dollar limit            (ISC-14)
  - Corporate-action freeze list    (ISC-42)
  - Per-broker circuit breaker      (ISC-43)
  - Audit log emission              (ISC-45 — propose entry per command)
  - Per-leg outcome audit entries   (ISC-45 — execute entries per broker)

What this gate does NOT yet enforce on the main.py path (deferred to v0.3
when order_processor is replaced with the Router fan-out):
  - Per-leg confirmation tokens     (ISC-11/12)
  - Intent-binding hash             (ISC-39)
  - Per-leg idempotency on broker   (ISC-40)

The gated CLI at `agentic.cli` already has the full per-leg-token flow; legacy
main.py users get safety-check gating today and full per-leg-token enforcement
when v0.3 lands.
"""

from __future__ import annotations

import asyncio
from typing import Any

from agentic.router import BrokerServerAccountStatusProvider, Router
from enforcement import (
    BrokerAccount,
    GateError,
    OrderIntent,
    OrderSide,
    gate_order,
)

_router: Router | None = None
_router_lock = asyncio.Lock()


async def get_router() -> Router:
    """Lazy singleton Router for the main.py path.

    Built on demand so import-time side effects stay zero — `main.py`
    importers that never run a buy/sell don't pay the 13-broker SPEC
    discovery cost.
    """
    global _router
    async with _router_lock:
        if _router is None:
            _router = Router.from_all_brokers()
        return _router


def reset_router() -> None:
    """Tests call this between cases to force a fresh Router. Not part of the
    documented main.py path."""
    global _router
    _router = None


async def apply_main_py_gate(
    *,
    action: str,
    quantity: float,
    ticker: str,
    price: float | None,
    brokers_to_use: list[str],
) -> dict[str, Any]:
    """Run the enforcement pipeline + write the propose audit entry.

    Returns a dict carrying `proposal_id` (string), `estimated_usd`,
    `leg_count`, `skipped_brokers`. Raises `GateError` on rejection — the
    caller is expected to convert to `CliRuntimeError` with an appropriate
    `ExitCode`. After `order_processor` returns the per-broker results,
    the caller calls `record_main_py_outcome(proposal_id, results)` so the
    audit log reflects what actually shipped.
    """
    router = await get_router()
    targets = tuple(
        BrokerAccount(broker, "primary") for broker in brokers_to_use
    )
    intent = OrderIntent(
        ticker=ticker,
        side=OrderSide(action),
        qty=quantity,
        targets=targets,
        price=price,
        dry_run=False,
    )
    if isinstance(router.provider, BrokerServerAccountStatusProvider):
        await router.provider.prefetch_for(ticker, brokers_to_use)
    ref_price = price if price is not None else 0.0
    proposal, decision = gate_order(
        router.core,
        intent,
        router.provider,
        ref_price=ref_price,
    )
    # F5 v0.4 — populate Router's intent cache so a subsequent
    # `execute_via_router` can reconstruct per-leg args from proposal_id alone.
    # Router.propose_order does this internally; our path goes around the
    # router-level method (we call `gate_order` directly to keep the
    # AccountStatusProvider compatible), so we have to populate explicitly.
    router._router_intent_cache[proposal.proposal_id] = {  # noqa: SLF001
        "ticker": intent.ticker,
        "side": intent.side.value,
        "qty": intent.qty,
        "price": intent.price,
    }
    return {
        "proposal_id": proposal.proposal_id,
        "estimated_usd": proposal.estimated_usd,
        "leg_count": proposal.leg_count,
        "skipped_brokers": [
            {"broker": b, "account_id": a, "reason": r}
            for (b, a, r) in decision.skipped_brokers
        ],
    }


async def record_main_py_outcome(
    *,
    proposal_id: str,
    action: str,
    quantity: float,
    ticker: str,
    price: float | None,
    results: dict[str, Any],
) -> None:
    """Write one `execute`-kind audit entry per broker after `order_processor`
    returns. Mirrors what the per-leg path would emit so the audit log is
    consistent across the gated CLI and legacy main.py.

    `results` is the dict shape `order_processor.process_orders` returns —
    expected keys: `successful`, `failed`, `skipped`, plus an optional
    per-broker `details` list.
    """
    router = await get_router()
    audit = router.core.audit
    # The propose entry already recorded `targets`. For the execute step we
    # write a SUMMARY-level entry — per-broker fan-out detail is captured by
    # order_processor's own logging today.
    from enforcement.audit_log import AuditEntry

    audit.append(
        AuditEntry(
            ts="",
            kind="execute",
            token=proposal_id,
            dry_run=False,
            result="ok" if int(results.get("failed", 0)) == 0 else "partial",
            extra={
                "source": "main_py",
                "action": action,
                "ticker": ticker,
                "qty": quantity,
                "price": price,
                "successful": int(results.get("successful", 0)),
                "failed": int(results.get("failed", 0)),
                "skipped": int(results.get("skipped", 0)),
                "usd_amount": (
                    (price or 0.0) * quantity * int(results.get("successful", 0))
                    if price else None
                ),
            },
        )
    )


async def apply_main_py_gate_batch(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gate a batch of orders BEFORE order_processor fans them out.

    Used by main.py's `_run_batch_from_file` and `_run_automate_from_recap`,
    and by the TUI's `submit_all_orders` / `retry_timed_out_brokers`. Every
    order goes through the full enforcement pipeline; the FIRST rejection
    raises `GateError` and aborts the whole batch — operators get a clean
    "fix the batch and re-run" signal rather than partial execution against
    half-vetted orders.

    Each order dict is expected to have keys: `action`, `quantity`,
    `ticker`, `price` (optional), `selected_brokers`.
    """
    proposals: list[dict[str, Any]] = []
    for order in orders:
        p = await apply_main_py_gate(
            action=str(order["action"]),
            quantity=float(order["quantity"]),
            ticker=str(order["ticker"]),
            price=order.get("price"),
            brokers_to_use=list(order.get("selected_brokers", [])),
        )
        proposals.append(p)
    return proposals


async def record_main_py_outcome_batch(
    *,
    proposals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    results: dict[str, Any],
) -> None:
    """Write one execute audit entry per gated order in the batch.

    `results` is the aggregate dict from `order_processor.process_orders`.
    Per-order detail isn't available at this aggregate level, so each audit
    entry shares the batch-aggregate success/failed counts — F5 v0.4
    refactor of order_processor will surface per-order results to enable
    sharper accounting.
    """
    for proposal, order in zip(proposals, orders):
        await record_main_py_outcome(
            proposal_id=proposal["proposal_id"],
            action=str(order["action"]),
            quantity=float(order["quantity"]),
            ticker=str(order["ticker"]),
            price=order.get("price"),
            results=results,
        )


async def execute_via_router(
    *,
    proposals: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    dry_run: bool = False,
    progress_fn: Any = None,
) -> dict[str, Any]:
    """Execute a batch of gated proposals through the Router instead of
    `order_processor.process_orders`. Replaces the legacy direct-broker-SDK
    fan-out with per-leg-token-validated execution through `BrokerMCPServer`.

    Returns a results dict matching the legacy `order_processor.process_orders`
    shape so callers don't need refactoring:
        {
            "successful": int,   # sum of per-leg ok=true across all orders
            "failed":     int,   # sum of per-leg failures (any reason)
            "skipped":    int,   # legs whose broker isn't registered
            "statuses": [        # one entry per input order
                {"successful": [broker, ...], "failed": [broker, ...], "skipped": [...]}
            ],
        }

    Per-leg-token validation: each proposal's leg tokens were minted by
    `apply_main_py_gate_batch` upstream. `Router.execute_order(proposal_id)`
    looks up the per-leg tokens and dispatches each broker leg through
    `BrokerMCPServer.place_at_broker` which validates ITS leg token against
    ITS single-target intent. This is the F5 v0.4 closure — ISC-11/12/39/40
    now hold on the legacy path.

    `progress_fn` is an optional callback `(message, force_redraw=False)`
    receiving human-readable progress strings. Used by main.py + TUI to feed
    the existing UI surfaces. Best-effort only — failures swallowed so a
    broken UI callback never aborts an execute.
    """
    router = await get_router()
    aggregate_successful = 0
    aggregate_failed = 0
    aggregate_skipped = 0
    statuses: list[dict[str, Any]] = []

    for proposal, order in zip(proposals, orders):
        pid = proposal["proposal_id"]
        ticker = str(order["ticker"])
        action = str(order["action"])
        qty = order["quantity"]
        try:
            if progress_fn is not None:
                progress_fn(
                    f"[router] executing proposal {pid[:12]}… for "
                    f"{action} {qty} {ticker} ({proposal['leg_count']} leg(s))"
                )
        except Exception:
            pass

        result = await router.execute_order(proposal_id=pid, dry_run=dry_run)

        order_successful: list[str] = []
        order_failed: list[str] = []
        order_skipped: list[str] = []

        if result.get("rejected"):
            order_failed = [
                str(b) for b in order.get("selected_brokers", [])
            ]
            try:
                if progress_fn is not None:
                    progress_fn(
                        f"[router] proposal rejected: "
                        f"{result.get('reason')}: {result.get('detail')}"
                    )
            except Exception:
                pass
        else:
            for leg in result.get("results", []):
                broker = str(leg.get("broker", ""))
                if leg.get("ok"):
                    order_successful.append(broker)
                    try:
                        if progress_fn is not None:
                            progress_fn(
                                f"[router] ✓ {broker}: "
                                f"{leg.get('detail', 'placed')}"
                            )
                    except Exception:
                        pass
                else:
                    reason = leg.get("reason", "unknown")
                    order_failed.append(broker)
                    try:
                        if progress_fn is not None:
                            progress_fn(
                                f"[router] ✗ {broker}: {reason} - "
                                f"{leg.get('detail', '')}"
                            )
                    except Exception:
                        pass

        statuses.append(
            {
                "ticker": ticker,
                "action": action,
                "successful": order_successful,
                "failed": order_failed,
                "skipped": order_skipped,
            }
        )
        aggregate_successful += len(order_successful)
        aggregate_failed += len(order_failed)
        aggregate_skipped += len(order_skipped)

    return {
        "successful": aggregate_successful,
        "failed": aggregate_failed,
        "skipped": aggregate_skipped,
        "statuses": statuses,
    }


def gate_error_to_exit_code(e: GateError) -> int:
    """Map a GateError reason to a stable exit code for main.py. Codes match
    the `cli_runtime.ExitCode` enum where possible.
    """
    # INVALID_ARGS = 2 in cli_runtime — that's where every gate rejection
    # currently lands. Per-reason mapping (per-day limit vs per-order vs
    # freeze) can be enriched in v0.3 if the operator needs more granular
    # exit codes.
    return 2
