"""ExecutionEngine — fan-out across per-broker MCP servers.

Historically this class was named ``Router``. ADR 0006 renames it to
``ExecutionEngine`` to reflect that it is the core execution engine the
CLI/TUI/MCP all sit on, not an agent-only surface. ``Router`` remains a
module-level alias for back-compat until callers are repointed.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from brokers import registry
from execution.in_process import BrokerMCPServer, BrokerMCPSpec, build_broker_mcp_spec
from execution.ports import BrokerPort
from execution.telemetry import logged_tool
from enforcement import (
    AccountStatusProvider,
    BrokerAccount,
    EnforcementCore,
    GateError,
    OrderIntent,
    OrderSide,
    gate_order,
)

if TYPE_CHECKING:
    from signals.nasdaq import CalendarSignal

DEFAULT_PLACEHOLDER_ACCOUNT_ID = "primary"
DEFAULT_RSA_STORE_PATH = "logs/automation.sqlite3"
DEFAULT_AUTOMATION_STORE_PATH = "logs/automation.sqlite3"


class NullAccountStatusProvider:
    """Stand-in returning permissive defaults — used by tests + as the inner
    fallback inside `BrokerServerAccountStatusProvider` for fields that
    individual brokers don't expose.
    """

    def get_settled_cash(self, broker: str, account_id: str) -> float:
        return 10_000_000.0

    def get_day_trades_in_window(self, broker: str, account_id: str) -> int:
        return 0

    def get_observed_qty(self, broker: str, account_id: str, ticker: str) -> float:
        return 0.0


class BrokerServerAccountStatusProvider:
    """Real-ish provider that reads `observed_qty` through each broker server's
    `get_holdings_at_broker(ticker)` call. `settled_cash` and `day_trades` are
    still permissive defaults — broker SDKs vary and full wiring is broker-
    specific (Robinhood `account_info`, Tradier `/accounts/{id}/balances`,
    etc.); individual broker modules can override the corresponding lookups
    in a v0.3 enrichment pass without changing the provider Protocol.

    The provider methods are synchronous (the `AccountStatusProvider` Protocol
    requires sync), so this class caches the broker holdings result inside an
    in-memory map populated by an async pre-fetch step (`prefetch_for(...)`)
    that the router calls before invoking `gate_order`.
    """

    def __init__(self, broker_servers: dict[str, BrokerPort]):
        self._broker_servers = broker_servers
        self._observed: dict[tuple[str, str, str], float] = {}

    async def prefetch_for(self, ticker: str, brokers: list[str]) -> None:
        """Populate the observed_qty cache for one ticker across selected brokers.

        The router calls this BEFORE `gate_order` so the synchronous
        `get_observed_qty` callback inside the gate can return real numbers.
        Cache lives for the lifetime of the request — re-prefetch each call.
        """
        async def _fetch(name: str) -> tuple[str, Any]:
            srv = self._broker_servers[name]
            return name, await srv.get_holdings_at_broker(ticker)

        results = await asyncio.gather(
            *(_fetch(n) for n in brokers if n in self._broker_servers),
            return_exceptions=True,
        )
        for entry in results:
            if isinstance(entry, Exception):
                continue
            name, holdings = entry
            qty = _coerce_qty(holdings, ticker)
            # We do not yet know which account_id this qty came from; the
            # account_id keyed here is the broker's own enumeration. For
            # single-account brokers this is "primary". Multi-account brokers
            # need richer per-account holdings shapes — flagged for v0.3.
            self._observed[(name, "primary", ticker.upper())] = qty

    def get_settled_cash(self, broker: str, account_id: str) -> float:
        return 10_000_000.0

    def get_day_trades_in_window(self, broker: str, account_id: str) -> int:
        return 0

    def get_observed_qty(self, broker: str, account_id: str, ticker: str) -> float:
        return float(self._observed.get((broker, account_id, ticker.upper()), 0.0))


_CREDENTIAL_KEY_STEMS = (
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
    "cookie",
)


def sanitize_holdings(payload: Any) -> Any:
    """Strip credential-shaped keys from broker holdings before they cross the
    MCP boundary (ISC-16). Recurses into nested dicts; lists are walked. The
    broker SDK may internally hold credential-shaped fields but the agentic
    response MUST NOT echo them.
    """
    if isinstance(payload, dict):
        cleaned: dict[Any, Any] = {}
        for k, v in payload.items():
            key_str = str(k).lower()
            if any(stem in key_str for stem in _CREDENTIAL_KEY_STEMS):
                continue
            cleaned[k] = sanitize_holdings(v)
        return cleaned
    if isinstance(payload, list):
        return [sanitize_holdings(item) for item in payload]
    return payload


def _coerce_qty(holdings: Any, ticker: str) -> float:
    """Best-effort extraction of a numeric qty for `ticker` from the variable
    shapes broker `get_holdings()` functions return. Returns 0.0 on miss.

    Broker SDKs return everything from `{"TSLA": 10}` to nested dicts to lists.
    This helper accepts the common shapes; broker modules can refine by
    supplying a more specific `holdings_fn` if needed.
    """
    if holdings is None:
        return 0.0
    key = ticker.upper()
    if isinstance(holdings, dict):
        if key in holdings:
            val = holdings[key]
            if isinstance(val, (int, float)):
                return float(val)
            if isinstance(val, dict) and "qty" in val:
                v = val["qty"]
                if isinstance(v, (int, float)):
                    return float(v)
        # Walk one level for nested dicts keyed by account
        for sub in holdings.values():
            if isinstance(sub, dict) and key in sub:
                v = sub[key]
                if isinstance(v, (int, float)):
                    return float(v)
    return 0.0


def load_all_broker_specs() -> dict[str, BrokerMCPSpec]:
    """Build a runtime ``BrokerMCPSpec`` for every enabled broker in the registry.

    Building a spec resolves the broker's function refs (importing that broker
    module). It happens here, not at module-load, so a single broker SDK import
    failure doesn't bring down the whole router — failing brokers are skipped
    and simply don't appear in the fan-out / `list_brokers` health output.
    """
    specs: dict[str, BrokerMCPSpec] = {}
    for spec in registry.enabled_specs():
        try:
            specs[spec.name] = build_broker_mcp_spec(spec)
        except Exception:  # noqa: BLE001 — keep the router functional on partial failures
            continue
    return specs


@dataclass
class ExecutionEngine:
    """In-process engine holding one `BrokerMCPServer` per enabled broker."""

    broker_servers: dict[str, BrokerPort]
    core: EnforcementCore
    provider: AccountStatusProvider
    rsa_store_path: str = DEFAULT_RSA_STORE_PATH
    automation_store_path: str = DEFAULT_AUTOMATION_STORE_PATH
    # Injectable for tests; None = fetch from the real Nasdaq calendar.
    calendar_fetcher: Callable[[], Awaitable[list["CalendarSignal"]]] | None = None

    @classmethod
    def from_all_brokers(
        cls,
        core: EnforcementCore | None = None,
        provider: AccountStatusProvider | None = None,
    ) -> "ExecutionEngine":
        specs = load_all_broker_specs()
        c = core or EnforcementCore.from_default_paths()
        servers = {name: BrokerMCPServer(spec, core=c) for name, spec in specs.items()}
        return cls(
            broker_servers=servers,
            core=c,
            # Default to BrokerServerAccountStatusProvider so observed_qty
            # is real (read through the broker server); settled_cash + PDT
            # remain permissive defaults until per-broker enrichment lands.
            provider=provider or BrokerServerAccountStatusProvider(servers),
        )

    def _resolve_brokers(self, brokers: list[str] | str | None) -> list[str]:
        if brokers is None or brokers == "all" or brokers == ["all"]:
            return list(self.broker_servers.keys())
        if isinstance(brokers, str):
            brokers = [brokers]
        unknown = [b for b in brokers if b not in self.broker_servers]
        if unknown:
            raise GateError(f"unknown broker(s): {unknown}")
        return brokers

    def _open_rsa_store(self) -> Any:
        """Open an RsaStore against the configured sqlite path. Lazy import so
        the agentic package doesn't pull `rsa_store` into the import graph at
        module-load time."""
        from rsa_store import RsaStore  # type: ignore[import-untyped]

        return RsaStore(self.rsa_store_path)

    @logged_tool(tool="router.get_rsa_trade")
    async def get_rsa_trade(self, trade_id: int) -> dict[str, Any]:
        """Return the rsa_trades row + all rsa_positions rows + current
        sweep_state per position. The agent reads this to know what to
        sweep / sell against. Read-only, no enforcement gate needed."""
        store = self._open_rsa_store()
        try:
            trade_row = store.get_trade(trade_id)
            if trade_row is None:
                return {"ok": False, "reason": "trade_not_found", "trade_id": trade_id}
            positions = store.list_positions(trade_id)
            return {
                "ok": True,
                "trade_id": int(trade_row["id"]),
                "ticker": trade_row["ticker"],
                "split_ratio": trade_row["split_ratio"],
                "expected_split_date": trade_row["expected_split_date"],
                "notes": trade_row["notes"],
                "created_at": trade_row["created_at"],
                "positions": [
                    {
                        "position_id": int(p["id"]),
                        "broker": p["broker"],
                        "account_id": p["account_id"],
                        "pre_split_qty": int(p["pre_split_qty"]),
                        "sweep_status": p["status"] if "status" in p.keys() else None,
                        "observed_qty": p["observed_qty"] if "observed_qty" in p.keys() else None,
                        "expected_post_qty": (
                            p["expected_post_qty"]
                            if "expected_post_qty" in p.keys() else None
                        ),
                        "last_checked": p["last_checked"] if "last_checked" in p.keys() else None,
                        "sold_at": p["sold_at"] if "sold_at" in p.keys() else None,
                    }
                    for p in positions
                ],
            }
        finally:
            store.close()

    @logged_tool(tool="router.run_sweep")
    async def run_sweep(
        self,
        trade_id: int,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Classify every position in an RSA trade against current broker
        holdings. v0.1 is read-only (dry_run only) — surfaces per-position
        status (`share_arrived`, `processing`, `awaiting_split`, etc.) so the
        agent can decide which positions are ready to sell. Live-sell on
        ARRIVED is v0.2 (will use propose_order/execute_order per ready leg).

        Per-broker `processing_window_days` from the existing
        `BROKER_PROFILES` table resolves AMBIGUOUS classifications when the
        `expected_split_date` + window has elapsed.
        """
        from datetime import date as _date  # local import — date already imported elsewhere
        from sweep import (  # type: ignore[import-untyped]
            BROKER_PROFILES,
            UNKNOWN_PROFILE,
            calculate_expected_post_qty,
            classify_holding,
            parse_ratio,
            resolve_ambiguous_with_date,
        )

        # Read the trade + positions via get_rsa_trade so MCP semantics stay
        # consistent (one source of truth for trade data shape).
        trade = await self.get_rsa_trade(trade_id)
        if not trade.get("ok"):
            return trade

        ticker = trade["ticker"]
        try:
            ratio_num, ratio_denom = parse_ratio(trade["split_ratio"])
        except Exception as e:
            return {
                "ok": False,
                "reason": "invalid_split_ratio",
                "detail": str(e),
                "trade_id": trade_id,
            }

        # Fan out get_holdings_at_broker for every broker that has a position
        brokers_in_trade = sorted({p["broker"] for p in trade["positions"]})
        per_broker_holdings: dict[str, Any] = {}
        for broker in brokers_in_trade:
            if broker not in self.broker_servers:
                per_broker_holdings[broker] = {"_error": "broker_not_registered"}
                continue
            try:
                per_broker_holdings[broker] = await self.broker_servers[
                    broker
                ].get_holdings_at_broker(ticker)
            except Exception as e:
                per_broker_holdings[broker] = {"_error": str(e)}

        today = _date.today()
        expected_split_date = trade.get("expected_split_date")

        classifications: list[dict[str, Any]] = []
        for position in trade["positions"]:
            broker = position["broker"]
            account_id = position["account_id"]
            pre_split_qty = int(position["pre_split_qty"])
            expected_post = calculate_expected_post_qty(
                pre_split_qty, ratio_num, ratio_denom
            )
            profile = BROKER_PROFILES.get(broker, UNKNOWN_PROFILE)

            broker_holdings = per_broker_holdings.get(broker)
            error = None
            observed_qty: float | None = None
            if isinstance(broker_holdings, dict) and broker_holdings.get("_error"):
                error = broker_holdings["_error"]
            else:
                observed_qty = _coerce_qty(broker_holdings, ticker)

            initial_status = classify_holding(
                observed_qty, pre_split_qty, expected_post
            )
            resolved_status = resolve_ambiguous_with_date(
                initial_status,
                expected_split_date,
                profile.processing_window_days,
                today,
            )
            classifications.append(
                {
                    "position_id": position["position_id"],
                    "broker": broker,
                    "account_id": account_id,
                    "pre_split_qty": pre_split_qty,
                    "expected_post_qty": expected_post,
                    "observed_qty": observed_qty,
                    "initial_status": str(initial_status.value),
                    "resolved_status": str(resolved_status.value),
                    "processing_window_days": profile.processing_window_days,
                    "error": error,
                }
            )

        summary = {
            "share_arrived": sum(
                1 for c in classifications if c["resolved_status"] == "share_arrived"
            ),
            "processing": sum(
                1 for c in classifications if c["resolved_status"] == "processing"
            ),
            "ambiguous": sum(
                1 for c in classifications if c["resolved_status"] == "ambiguous"
            ),
            "awaiting_split": sum(
                1 for c in classifications if c["resolved_status"] == "awaiting_split"
            ),
            "fractional_pending": sum(
                1
                for c in classifications
                if c["resolved_status"] == "fractional_pending"
            ),
            "error": sum(1 for c in classifications if c["error"]),
        }
        persisted: list[int] = []
        if not dry_run:
            # F3 v0.2 — write each classification to sweep_state via the
            # canonical `rsa_store.record_sweep` primitive (same path the
            # legacy `python3 main.py sweep --from-trade <id>` uses). The
            # sweep is now agent-driveable end-to-end without the CLI.
            from sweep import SweepStatus  # type: ignore[import-untyped]

            store = self._open_rsa_store()
            try:
                from datetime import datetime as _dt, UTC as _UTC

                observed_at = _dt.now(_UTC).isoformat()
                for c in classifications:
                    if c.get("error"):
                        continue
                    status_value = c["resolved_status"]
                    try:
                        status_enum = SweepStatus(status_value)
                    except ValueError:
                        continue
                    store.record_sweep(
                        position_id=int(c["position_id"]),
                        status=status_enum,
                        observed_qty=c["observed_qty"],
                        expected_post_qty=int(c["expected_post_qty"]),
                        observed_at=observed_at,
                        details=f"router-sweep status={status_value}",
                    )
                    persisted.append(int(c["position_id"]))
            finally:
                store.close()

        return {
            "ok": True,
            "trade_id": trade_id,
            "ticker": ticker,
            "split_ratio": trade["split_ratio"],
            "expected_split_date": expected_split_date,
            "today": today.isoformat(),
            "dry_run": dry_run,
            "classifications": classifications,
            "summary": summary,
            "would_sell": [
                c for c in classifications if c["resolved_status"] == "share_arrived"
            ],
            "persisted_position_ids": persisted,
        }

    @logged_tool(tool="router.recap_ingest")
    async def recap_ingest(self, recap_text: str) -> dict[str, Any]:
        """Parse a chat recap and persist all four signal tiers to the
        automation store.

        Returns a structured summary: `new_buy` + `new_research` + `new_tba`
        counts, plus the categorized lists so the agent can act on each
        tier without a second call. Storage uses the existing
        `AutomationRecapStore` schema (extended with research_signals +
        tba_candidates tables in this turn).

        The actionable buy signals (UPCOMING BUYS with date + ratio) flow
        through `get_due_buy_signals` into the existing automate path on the
        next scheduled run. The research + TBA tiers are watchlists the
        agent monitors and promotes (via `mark_research_promoted` /
        `mark_tba_promoted`) when subsequent recaps move them to UPCOMING.
        """
        from datetime import datetime as _dt

        from automation_recap import (  # type: ignore[import-untyped]
            AutomationRecapStore,
            parse_chat_recap_full,
        )

        result = parse_chat_recap_full(recap_text)
        store = AutomationRecapStore(self.automation_store_path)
        try:
            counts = store.record_recap_extended(recap_text, result, _dt.now())
        finally:
            store.close()

        return {
            "ok": True,
            "counts": counts,
            "upcoming": [
                {
                    "ticker": u.ticker,
                    "date_mmdd": u.date_mmdd,
                    "ratio": u.ratio,
                    "round_num": u.round_num,
                    "notes": u.notes,
                }
                for u in result.upcoming
            ],
            "stock_back": [
                {"ticker": s.ticker, "detail": s.detail, "brokers": s.brokers}
                for s in result.stock_back
            ],
            "research": [
                {
                    "ticker": r.ticker,
                    "date_mmdd": r.date_mmdd,
                    "notes": r.notes,
                }
                for r in result.research
            ],
            "tba": [
                {"ticker": t.ticker, "ratio": t.ratio, "notes": t.notes}
                for t in result.tba
            ],
        }

    @logged_tool(tool="router.scan_signals")
    async def scan_signals(self, refresh: bool = True) -> dict[str, Any]:
        """Scan the reverse-split calendar into calendar_signals (refresh=True)
        or just read staged 'new' signals (refresh=False). Read/ingest only —
        never proposes or executes orders.

        If the calendar fetch/parse step fails (network error, malformed
        payload, etc.), returns `{"ok": False, "error": ..., "source": ...}`
        instead of raising — a fetch failure is expected/routine, not fatal.
        """
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        now = _dt.now()
        store = AutomationRecapStore(self.automation_store_path)
        try:
            counts = {"new": 0, "seen": 0, "expired": 0}
            if refresh:
                from signals.nasdaq import (  # type: ignore[import-untyped]
                    SOURCE_NAME,
                    fetch_splits_calendar,
                    parse_splits_payload,
                )

                try:
                    if self.calendar_fetcher is not None:
                        signals = await self.calendar_fetcher()
                    else:
                        signals = parse_splits_payload(await fetch_splits_calendar())
                except Exception as exc:  # noqa: BLE001 — fetch/parse failure is routine, not fatal
                    return {"ok": False, "error": str(exc), "source": SOURCE_NAME}
                counts.update(
                    store.upsert_calendar_signals(signals, source=SOURCE_NAME, now=now)
                )
                counts["expired"] = store.expire_stale_calendar_signals(
                    today=now.date(), now=now
                )
            rows = [
                {key: row[key] for key in row.keys()}
                for row in store.list_calendar_signals(status="new")
            ]
            return {"ok": True, "counts": counts, "signals": rows}
        finally:
            store.close()

    @logged_tool(tool="router.dismiss_signal")
    async def dismiss_signal(self, signal_id: int, reason: str) -> dict[str, Any]:
        """Mark a 'new' calendar signal dismissed with a reason (audit trail)."""
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        store = AutomationRecapStore(self.automation_store_path)
        try:
            try:
                store.dismiss_calendar_signal(signal_id, reason=reason, now=_dt.now())
            except ValueError as exc:
                return {"ok": False, "signal_id": signal_id, "error": str(exc)}
            return {"ok": True, "signal_id": signal_id, "status": "dismissed"}
        finally:
            store.close()

    @logged_tool(tool="router.promote_signal")
    async def promote_signal(self, signal_id: int) -> dict[str, Any]:
        """Promote a calendar signal into the automate due-buy queue. The buy
        is NOT executed by this call, but it will be gated and executed on
        the next automate run — and a signal without an effective date
        becomes immediately due. Dismiss instead if unsure."""
        from datetime import datetime as _dt

        from automation_recap import AutomationRecapStore  # type: ignore[import-untyped]

        store = AutomationRecapStore(self.automation_store_path)
        try:
            try:
                buy_id = store.promote_calendar_signal(signal_id, now=_dt.now())
            except ValueError as exc:
                return {"ok": False, "signal_id": signal_id, "error": str(exc)}
            return {"ok": True, "signal_id": signal_id, "buy_signal_id": buy_id}
        finally:
            store.close()

    @logged_tool(tool="router.sell_arrived")
    async def sell_arrived(
        self,
        trade_id: int,
        price: float | None = None,
    ) -> dict[str, Any]:
        """One-shot: run a sweep and PROPOSE sells for every ARRIVED leg.

        Returns a `proposal_id` the agent passes to `execute_order` after
        principal review. Never auto-executes — live sells still require the
        explicit two-step propose/execute flow with `--live`.

        The returned proposal binds to a multi-leg fan-out where each leg is
        ONE arrived (broker, account_id, observed_qty) tuple. Each leg's
        quantity is the observed post-split quantity (not pre-split) — that
        IS what's available to sell. The price arg, if supplied, applies to
        every leg; otherwise the proposal is a market-order-shaped intent.
        """
        sweep_result = await self.run_sweep(trade_id, dry_run=True)
        if not sweep_result.get("ok"):
            return sweep_result
        arrived = sweep_result.get("would_sell", [])
        if not arrived:
            return {
                "ok": False,
                "reason": "no_arrived_positions",
                "trade_id": trade_id,
                "summary": sweep_result.get("summary"),
                "detail": "no positions classified as share_arrived; nothing to sell",
            }

        ticker = sweep_result["ticker"]

        # v0.4 — group arrived legs by their observed_qty. Each unique qty
        # becomes its own fan-out proposal (single OrderIntent must have one
        # qty across all targets). For the common case (all legs same qty)
        # this yields one proposal; for heterogeneous arrivals it yields N.
        qty_groups: dict[float, list[dict[str, Any]]] = {}
        for c in arrived:
            qty = float(c["observed_qty"])
            qty_groups.setdefault(qty, []).append(c)

        proposals: list[dict[str, Any]] = []
        ref_price = price if price is not None else 0.0
        for qty, group_legs in qty_groups.items():
            targets = tuple(
                BrokerAccount(c["broker"], c["account_id"]) for c in group_legs
            )
            intent = OrderIntent(
                ticker=ticker,
                side=OrderSide.SELL,
                qty=qty,
                targets=targets,
                price=price,
                dry_run=False,
            )
            try:
                proposal, decision = gate_order(
                    self.core,
                    intent,
                    self.provider,
                    ref_price=ref_price,
                )
            except GateError as e:
                # One group's rejection doesn't kill siblings — surface the
                # failure inline and keep proposing the rest.
                proposals.append(
                    {
                        "ok": False,
                        "qty": qty,
                        "reason": e.reason,
                        "detail": str(e),
                        "legs": [
                            {"broker": c["broker"], "account_id": c["account_id"]}
                            for c in group_legs
                        ],
                    }
                )
                continue
            proposals.append(
                {
                    "ok": True,
                    "qty": qty,
                    "proposal_id": proposal.proposal_id,
                    "valid_until_ts": proposal.valid_until_ts,
                    "estimated_usd": proposal.estimated_usd,
                    "leg_count": proposal.leg_count,
                    "legs": [
                        {"broker": c["broker"], "account_id": c["account_id"]}
                        for c in group_legs
                    ],
                    "skipped_brokers": [
                        {"broker": b, "account_id": a, "reason": r}
                        for (b, a, r) in decision.skipped_brokers
                    ],
                }
            )

        successful_proposals = [p for p in proposals if p.get("ok")]
        return {
            "ok": bool(successful_proposals),
            "trade_id": trade_id,
            "ticker": ticker,
            "side": "sell",
            "price": price,
            "proposals": proposals,
            "proposal_count": len(successful_proposals),
            "total_estimated_usd": sum(
                float(p.get("estimated_usd", 0.0))
                for p in successful_proposals
            ),
            "total_legs": sum(
                int(p.get("leg_count", 0)) for p in successful_proposals
            ),
            "arrived_positions": [
                {"broker": c["broker"], "account_id": c["account_id"]}
                for c in arrived
            ],
        }

    @logged_tool(tool="router.list_brokers")
    async def list_brokers(self) -> dict[str, Any]:
        """Aggregate per-broker health into a single response."""
        results = await asyncio.gather(
            *(srv.health_check() for srv in self.broker_servers.values()),
            return_exceptions=True,
        )
        items: list[dict[str, Any]] = []
        for name, res in zip(self.broker_servers.keys(), results):
            if isinstance(res, Exception):
                items.append({"broker": name, "ok": False, "error": str(res)})
            else:
                items.append(res)
        return {"brokers": items, "count": len(items)}

    @logged_tool(tool="router.get_holdings")
    async def get_holdings(
        self,
        ticker: str | None = None,
        brokers: list[str] | str | None = None,
    ) -> dict[str, Any]:
        """Fan out `get_holdings_at_broker` across selected brokers in parallel."""
        names = self._resolve_brokers(brokers)
        results = await asyncio.gather(
            *(self.broker_servers[n].get_holdings_at_broker(ticker) for n in names),
            return_exceptions=True,
        )
        per_broker: list[dict[str, Any]] = []
        for name, res in zip(names, results):
            if isinstance(res, Exception):
                per_broker.append({"broker": name, "ok": False, "error": str(res)})
            else:
                # ISC-16: scrub credential-shaped keys from broker SDK payload
                # before crossing the MCP boundary.
                per_broker.append(
                    {"broker": name, "ok": True, "holdings": sanitize_holdings(res)}
                )
        return {"ticker": ticker, "brokers": per_broker}

    async def _discover_accounts(
        self, brokers: list[str]
    ) -> dict[str, list[str]]:
        """Per-broker account_ids — one MCP call per broker, parallel."""
        async def _one(name: str) -> tuple[str, list[str]]:
            return name, await self.broker_servers[name].list_accounts_at_broker()

        results = await asyncio.gather(
            *(_one(n) for n in brokers), return_exceptions=True
        )
        out: dict[str, list[str]] = {}
        for r in results:
            if isinstance(r, Exception):
                continue
            name, accounts = r
            out[name] = list(accounts) if accounts else [DEFAULT_PLACEHOLDER_ACCOUNT_ID]
        return out

    def _build_intent(
        self,
        *,
        ticker: str,
        side: str,
        qty: float,
        accounts_by_broker: dict[str, list[str]],
        price: float | None,
        dry_run: bool,
    ) -> OrderIntent:
        targets = tuple(
            BrokerAccount(name, account_id)
            for name, accounts in accounts_by_broker.items()
            for account_id in accounts
        )
        return OrderIntent(
            ticker=ticker,
            side=OrderSide(side),
            qty=qty,
            targets=targets,
            price=price,
            dry_run=dry_run,
        )

    @logged_tool(tool="router.propose_order")
    async def propose_order(
        self,
        *,
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | str | None = None,
        price: float | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Mint a confirmation token bound to this intent.

        Discovers accounts per broker (multi-account fan-out), pre-fetches
        observed_qty via the provider, then runs the full enforcement pipeline
        (freeze list, per-order limit, daily limit, circuit breaker,
        reconciliation, settled-cash filter, audit-log emission) and returns
        the token + estimate. The returned `confirmation_token` binds to the
        EXACT discovered targets — execute_order must call back with the same
        broker list or the intent_hash diverges.
        """
        names = self._resolve_brokers(brokers)
        accounts_by_broker = await self._discover_accounts(names)
        if isinstance(self.provider, BrokerServerAccountStatusProvider):
            await self.provider.prefetch_for(ticker, list(accounts_by_broker.keys()))
        intent = self._build_intent(
            ticker=ticker,
            side=side,
            qty=qty,
            accounts_by_broker=accounts_by_broker,
            price=price,
            dry_run=dry_run,
        )
        ref_price = price if price is not None else 0.0
        proposal, decision = gate_order(
            self.core,
            intent,
            self.provider,
            ref_price=ref_price,
        )
        return {
            "proposal_id": proposal.proposal_id,
            "valid_until_ts": proposal.valid_until_ts,
            "ttl_seconds": self.core.proposal_ttl_seconds,
            "estimated_usd": proposal.estimated_usd,
            "brokers": names,
            "accounts_by_broker": accounts_by_broker,
            "leg_count": proposal.leg_count,
            "ticker": intent.ticker,
            "side": intent.side.value,
            "qty": intent.qty,
            "price": intent.price,
            "dry_run": intent.dry_run,
            "skipped_brokers": [
                {"broker": b, "account_id": a, "reason": r}
                for (b, a, r) in decision.skipped_brokers
            ],
        }

    @logged_tool(tool="router.execute_order")
    async def execute_order(
        self,
        *,
        proposal_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Fan out the live order using a previously minted fan-out proposal.

        v0.3 per-leg-token model:
          1. Look up the Proposal by `proposal_id` — pulls all N LegProposals.
          2. Dispatch each leg via `BrokerMCPServer.place_at_broker` with that
             leg's own token. The broker validates the leg token against its
             single-target intent (consumed atomically inside the gate).
          3. Per-leg failures isolate; one leg's intent_mismatch or
             token_already_used does NOT halt sibling legs.

        The agent supplies ONLY `proposal_id` and `dry_run` — ticker, qty,
        side, brokers, accounts, and the matching intent_hash are looked up
        from the master Proposal record. This makes execute idempotent on
        the agent side: the same proposal_id reliably names the same intent.
        """
        proposal = self.core.proposal_store.get_proposal(proposal_id)
        if proposal is None:
            return {
                "proposal_id": proposal_id,
                "dry_run": dry_run,
                "results": [],
                "success_count": 0,
                "failure_count": 0,
                "rejected": True,
                "reason": "proposal_not_found",
                "detail": f"no proposal with id {proposal_id}",
            }

        if not proposal.legs:
            return {
                "proposal_id": proposal_id,
                "dry_run": dry_run,
                "results": [],
                "success_count": 0,
                "failure_count": 0,
                "rejected": True,
                "reason": "empty_proposal",
                "detail": "proposal has no legs",
            }

        # The Proposal is self-describing (ADR 0005): it carries the order
        # params it authorizes, so execute is sufficient from the durable store
        # alone — no router-side intent cache, works across restarts and across
        # processes. The per-leg intent_hash remains the security authority.
        ticker = proposal.ticker
        qty = proposal.qty
        side = proposal.side.value
        price = proposal.price

        # dry_run is bound into each leg's intent_hash, so a proposal is born
        # live-or-dry. Enforce that the caller's dry_run matches what was minted
        # rather than silently flipping the order's mode (or failing every leg
        # with an opaque intent_mismatch).
        if dry_run != proposal.dry_run:
            return {
                "proposal_id": proposal_id,
                "dry_run": dry_run,
                "results": [],
                "success_count": 0,
                "failure_count": 0,
                "rejected": True,
                "reason": "dry_run_mismatch",
                "detail": (
                    f"proposal minted with dry_run={proposal.dry_run}; "
                    f"execute called with dry_run={dry_run}"
                ),
            }

        legs = await asyncio.gather(
            *(
                self.broker_servers[leg.broker].place_at_broker(
                    ticker=ticker,
                    qty=qty,
                    side=side,
                    price=price,
                    account_id=leg.account_id,
                    dry_run=dry_run,
                    confirmation_token=leg.token,
                )
                for leg in proposal.legs
            ),
            return_exceptions=True,
        )
        results: list[dict[str, Any]] = []
        success_count = 0
        failure_count = 0
        for leg_proposal, leg in zip(proposal.legs, legs):
            name = leg_proposal.broker
            if isinstance(leg, Exception):
                results.append({"broker": name, "ok": False, "error": str(leg)})
                failure_count += 1
            else:
                r = {
                    "broker": leg.broker,
                    "account_id": leg.account_id,
                    "ok": leg.ok,
                    "dry_run": leg.dry_run,
                    "idempotency_key": leg.idempotency_key,
                    "reason": leg.reason,
                    "detail": leg.detail,
                }
                results.append(r)
                if leg.ok:
                    success_count += 1
                else:
                    failure_count += 1
        return {
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "dry_run": dry_run,
            "results": results,
            "success_count": success_count,
            "failure_count": failure_count,
        }

    @logged_tool(tool="router.place_order")
    async def place_order(
        self,
        *,
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | str | None = None,
        price: float | None = None,
        dry_run: bool = True,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Convenience: propose-then-execute in one call.

        - With no proposal_id + dry_run=True: propose + execute as dry-run preview.
        - With no proposal_id + dry_run=False: REJECTED (live order requires
          the explicit two-step propose / execute flow).
        - With proposal_id: skip propose, execute against the stored proposal.
        """
        if proposal_id is None and not dry_run:
            raise GateError(
                "live place_order requires a proposal_id from a prior "
                "propose_order call; auto-confirmed single-call live orders are "
                "explicitly forbidden (ISC-18)"
            )
        if proposal_id is None:
            proposal = await self.propose_order(
                ticker=ticker,
                qty=qty,
                side=side,
                brokers=brokers,
                price=price,
                dry_run=True,
            )
            proposal_id = proposal["proposal_id"]
        return await self.execute_order(
            proposal_id=proposal_id,
            dry_run=dry_run,
        )


# Back-compat alias (ADR 0006): the class was renamed Router → ExecutionEngine.
# Callers (cli_bridge, agentic.cli, tests, the MCP entrypoint) still import
# `Router`; this keeps them working until they are repointed in later steps.
Router = ExecutionEngine


def build_router_fastmcp_server(router: ExecutionEngine) -> Any:
    """Wrap an ExecutionEngine into a FastMCP server with the agent-facing tools."""
    from mcp.server.fastmcp import FastMCP

    app = FastMCP(name="ssg-router")

    @app.tool()
    async def list_brokers() -> dict[str, Any]:
        """List enabled brokers with health, MFA, fractional-support flags."""
        return await router.list_brokers()

    @app.tool()
    async def get_holdings(
        ticker: str | None = None,
        brokers: list[str] | None = None,
    ) -> dict[str, Any]:
        """Fan out a holdings query across selected brokers (or all by default)."""
        return await router.get_holdings(ticker=ticker, brokers=brokers)

    @app.tool()
    async def get_rsa_trade(trade_id: int) -> dict[str, Any]:
        """Return the rsa_trades row + all rsa_positions rows for an RSA trade.

        Read-only — the agent uses this to know what ticker, ratio, expected
        split date, and per-broker pre-split quantities are in flight.
        """
        return await router.get_rsa_trade(trade_id=trade_id)

    @app.tool()
    async def run_sweep(trade_id: int, dry_run: bool = True) -> dict[str, Any]:
        """Classify every position in an RSA trade against current broker
        holdings. Returns per-position `resolved_status` (share_arrived,
        processing, ambiguous, awaiting_split, fractional_pending, error)
        and a summary count.

        v0.2: `dry_run=False` ALSO writes each classification to `sweep_state`
        via the canonical `rsa_store.record_sweep` primitive — the agentic
        sweep is now feature-parity with the legacy `python3 main.py sweep
        --from-trade <id>` CLI.
        """
        return await router.run_sweep(trade_id=trade_id, dry_run=dry_run)

    @app.tool()
    async def recap_ingest(recap_text: str) -> dict[str, Any]:
        """Parse a chat recap and persist all four signal tiers.

        Tiers:
          1. UPCOMING BUYS (date + ratio known) → buy_signals → due-buy queue
          2. STOCKS BACK AND LATEST → stock_back_state → pending sell triggers
          3. RESEARCH POSTED (date soft) → research_signals (watchlist)
          4. TBA (ratio/date pending) → tba_candidates (long watchlist)

        Returns counts + the categorized lists so the agent can act on
        each tier without a second call.
        """
        return await router.recap_ingest(recap_text=recap_text)

    @app.tool()
    async def scan_signals(refresh: bool = True) -> dict[str, Any]:
        """Scan the Nasdaq reverse-split calendar into the signal store
        (refresh=True) or read staged 'new' signals (refresh=False).
        Read/ingest only — never trades. Evaluate each returned signal and
        either promote_signal (worth playing) or dismiss_signal (with reason).

        Signals with a null effective_date become immediately due if
        promoted — verify the real split date before promoting one.

        If the calendar fetch/parse step fails, returns
        `{"ok": False, "error": ..., "source": ...}` instead of raising.
        """
        return await router.scan_signals(refresh=refresh)

    @app.tool()
    async def dismiss_signal(signal_id: int, reason: str) -> dict[str, Any]:
        """Dismiss a staged calendar signal that isn't worth playing. Always
        give a concrete reason (e.g. 'ratio below 1:5', 'price exceeds
        per-order cap') — it's the audit trail for why the agent skipped a
        play. Only 'new' signals can be dismissed; returns ok=false with an
        error message otherwise."""
        return await router.dismiss_signal(signal_id=signal_id, reason=reason)

    @app.tool()
    async def promote_signal(signal_id: int) -> dict[str, Any]:
        """Promote a calendar signal into the automate due-buy queue. The buy
        is NOT executed by this call, but it will be gated and executed on
        the next automate run — and a signal without an effective date
        becomes immediately due. Dismiss instead if unsure."""
        return await router.promote_signal(signal_id=signal_id)

    @app.tool()
    async def sell_arrived(
        trade_id: int, price: float | None = None
    ) -> dict[str, Any]:
        """One-shot tool: run a sweep and PROPOSE sells for every leg
        classified as `share_arrived`. Returns a `proposal_id` the agent
        passes to `execute_order` after principal review. Live sells still
        require the explicit two-step propose/execute flow with `--live` —
        this tool only proposes.
        """
        return await router.sell_arrived(trade_id=trade_id, price=price)

    @app.tool()
    async def propose_order(
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | None = None,
        price: float | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Run safety gates and mint a single-use confirmation_token.

        Surface the returned token + estimate to the principal. The token
        expires (default 300s) and is bound to this exact intent — any change
        to ticker/qty/side/brokers/price invalidates it.
        """
        return await router.propose_order(
            ticker=ticker,
            qty=qty,
            side=side,
            brokers=brokers,
            price=price,
            dry_run=dry_run,
        )

    @app.tool()
    async def execute_order(
        proposal_id: str,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Place the order fan-out using a previously minted proposal_id.

        The router looks up the per-leg tokens from the proposal and
        dispatches each broker leg with its own single-use token. Per-leg
        failures isolate; sibling legs continue.
        """
        return await router.execute_order(
            proposal_id=proposal_id,
            dry_run=dry_run,
        )

    @app.tool()
    async def place_order(
        ticker: str,
        qty: float,
        side: str,
        brokers: list[str] | None = None,
        price: float | None = None,
        dry_run: bool = True,
        proposal_id: str | None = None,
    ) -> dict[str, Any]:
        """Convenience tool — proposes + executes a dry-run in one call, or
        executes with a supplied proposal_id. Live orders without a proposal_id
        are rejected.
        """
        return await router.place_order(
            ticker=ticker,
            qty=qty,
            side=side,
            brokers=brokers,
            price=price,
            dry_run=dry_run,
            proposal_id=proposal_id,
        )

    return app


def run_stdio() -> None:
    """`python -m agentic.router` entrypoint.

    Set `SSG_AGENTIC_SMOKE_ONLY=1` to print the broker health dict and exit
    without starting the stdio server — useful for CI / load-check scripts.
    """
    router = ExecutionEngine.from_all_brokers()
    if os.getenv("SSG_AGENTIC_SMOKE_ONLY") == "1":
        import json

        health = asyncio.run(router.list_brokers())
        print(json.dumps(health, indent=2))
        return
    app = build_router_fastmcp_server(router)
    app.run()
