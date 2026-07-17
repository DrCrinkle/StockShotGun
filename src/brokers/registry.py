"""Single source of truth for broker identity, metadata, and function bindings.

See ``docs/adr/0004-broker-registry-single-source.md``.

This module is **pure data + a lazy resolver**. Importing it imports *zero*
broker SDKs: trade/holdings/validate/session-getter references are stored as
``"module:symbol"`` strings and resolved on demand via :func:`importlib`.

Two properties fall out of that:

* A single-broker process (the agentic generic entrypoint
  ``python -m agentic.broker <name>``) can resolve only its own broker instead
  of importing all thirteen — the per-broker isolation ADR 0003 promised.
* It sidesteps the ``base.py`` <-> broker-module circular import that the old
  five-registry duplication existed to dodge: the registry never imports the
  broker modules at module load, so it can live alongside ``BrokerConfig``
  without a cycle.

Every other broker enumeration in the codebase (``BrokerConfig``, the session
manager, the TUI broker map, the agentic router's spec loader) derives from
:data:`BROKERS` here.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional


@dataclass(frozen=True)
class BrokerSpec:
    """Pure-data description of one broker. MCP-agnostic by design.

    Function bindings are lazy ``"module:symbol"`` strings, never live objects,
    so constructing/holding a ``BrokerSpec`` never imports broker code. Resolve
    them through the module-level ``resolve_*`` helpers.
    """

    name: str
    session_key: str
    env_vars: tuple[str, ...]
    trade: str  # e.g. "brokers.robinhood:robinTrade"
    holdings: str  # e.g. "brokers.robinhood:robinGetHoldings"
    session_getter: str  # e.g. "brokers.robinhood:get_robinhood_session"
    validate: Optional[str] = None  # None when the broker has no validate fn
    requires_mfa: bool = False
    supports_fractional: bool = False
    # True only for brokers whose TRADE FN can place on one specific account
    # per leg AND whose session payload exposes real per-account ids for
    # fan-out. The execution layer maps this flag to the
    # session_manager_accounts discovery closure (one leg per account_id);
    # everyone else fans out a single "primary" leg. Pair this with
    # `account_scoped_trade=True` (below) — a trade fn that is still
    # account-blind (`TradeFn(side, qty, ticker, price)`, no account
    # parameter) must NOT be paired with `multi_account=True`: per-account
    # legs on an account-blind fn multiply orders (N accounts -> N legs x
    # internal fan-out = N^2 live orders; final-review C1). Fennel is the
    # first (and, today, only) broker with both flags True — see its entry
    # below. Kept as a flag so this module imports no execution internals.
    multi_account: bool = False
    # True only when the resolved TRADE FN accepts an `account_id: str`
    # keyword arg and, when given one, places exactly ONE order for that
    # account (no internal fan-out over other accounts). The execution
    # layer (`execution/in_process.py:place_at_broker`) passes
    # `account_id=<leg's account>` to the trade fn ONLY when this flag is
    # True — every other (blind) trade fn is still called
    # `trade_fn(side, qty, ticker, price)` with no account kwarg, so
    # flipping this on for a broker whose trade fn doesn't accept the kwarg
    # is a hard TypeError at dispatch time, not a silent double-buy. A real
    # (non-"primary") account-id leg on a spec with this flag False fails
    # loudly instead of dispatching blind
    # (`reason="account_scoped_dispatch_unsupported"`). Fennel completed
    # this migration (ADR 0006 completion, P1 fix) — its trade fn now takes
    # an optional `account_id` kwarg and places a single order per call when
    # given one. Every other broker's trade fn remains account-blind, so
    # this stays False for all of them until they migrate the same way.
    account_scoped_trade: bool = False
    enabled: bool = True
    notes: str = ""


# Canonical broker order — matches the historical order across the old
# registries (BrokerConfig.BROKERS, ALL_BROKER_SUBPACKAGES). Insertion order is
# preserved by dict, so this list defines the order the router fans out in.
_SPECS: tuple[BrokerSpec, ...] = (
    BrokerSpec(
        name="Robinhood",
        session_key="robinhood",
        env_vars=("ROBINHOOD_USER", "ROBINHOOD_PASS", "ROBINHOOD_MFA"),
        trade="brokers.robinhood:robinTrade",
        holdings="brokers.robinhood:robinGetHoldings",
        validate="brokers.robinhood:robinValidate",
        session_getter="brokers.robinhood:get_robinhood_session",
        requires_mfa=True,
        notes="Username/password + MFA",
    ),
    BrokerSpec(
        name="Tradier",
        session_key="tradier",
        env_vars=("TRADIER_ACCESS_TOKEN",),
        trade="brokers.tradier:tradierTrade",
        holdings="brokers.tradier:tradierGetHoldings",
        validate="brokers.tradier:tradierValidate",
        session_getter="brokers.tradier:get_tradier_session",
        notes="API-token auth",
    ),
    BrokerSpec(
        name="TastyTrade",
        session_key="tastytrade",
        env_vars=("TASTY_CLIENT_ID", "TASTY_CLIENT_SECRET", "TASTY_REFRESH_TOKEN"),
        trade="brokers.tastytrade:tastyTrade",
        holdings="brokers.tastytrade:tastyGetHoldings",
        validate="brokers.tastytrade:tastyValidate",
        session_getter="brokers.tastytrade:get_tastytrade_session",
        notes="Username/password",
    ),
    BrokerSpec(
        name="Public",
        session_key="public",
        env_vars=("PUBLIC_API_SECRET",),
        trade="brokers.public:publicTrade",
        holdings="brokers.public:publicGetHoldings",
        session_getter="brokers.public:get_public_session",
        notes="API-token auth",
    ),
    BrokerSpec(
        name="Firstrade",
        session_key="firstrade",
        env_vars=("FIRSTRADE_USER", "FIRSTRADE_PASS", "FIRSTRADE_MFA"),
        trade="brokers.firstrade:firstradeTrade",
        holdings="brokers.firstrade:firstradeGetHoldings",
        validate="brokers.firstrade:firstradeValidate",
        session_getter="brokers.firstrade:get_firstrade_session",
        requires_mfa=True,
        notes="Username/password + MFA",
    ),
    BrokerSpec(
        name="Fennel",
        session_key="fennel",
        env_vars=("FENNEL_ACCESS_TOKEN",),
        trade="brokers.fennel:fennelTrade",
        holdings="brokers.fennel:fennelGetHoldings",
        session_getter="brokers.fennel:get_fennel_session",
        # ADR 0006 completion (P1 fix): `fennelTrade` now accepts an
        # optional `account_id` kwarg (`brokers/fennel.py`) and, when given
        # one, places exactly ONE order for that account instead of looping
        # over every session account_id. That closes the enforcement-
        # accounting gap where `propose_order` gated ONE "primary" leg while
        # the broker call placed N live orders (N = account count) — every
        # safety number (estimate, per-order/daily limits, audit) was
        # understated by N. `multi_account=True` now drives real per-account
        # leg discovery (`make_session_accounts_fn`) and
        # `account_scoped_trade=True` tells `place_at_broker` to dispatch
        # each leg with its own `account_id` — one gated leg per live order,
        # 1:1. (Previously pinned `multi_account=False` + internal fan-out
        # as a stopgap; that shape is retired — see
        # `test_multi_account_discovery.py`'s superseded golden.)
        multi_account=True,
        account_scoped_trade=True,
        notes="Personal access token",
    ),
    BrokerSpec(
        name="Schwab",
        session_key="schwab",
        env_vars=(
            "SCHWAB_API_KEY",
            "SCHWAB_API_SECRET",
            "SCHWAB_CALLBACK_URL",
            "SCHWAB_TOKEN_PATH",
        ),
        trade="brokers.schwab:schwabTrade",
        holdings="brokers.schwab:schwabGetHoldings",
        validate="brokers.schwab:schwabValidate",
        session_getter="brokers.schwab:get_schwab_session",
        notes="OAuth 2.0; token cached in tokens/",
    ),
    BrokerSpec(
        name="BBAE",
        session_key="bbae",
        env_vars=("BBAE_USER", "BBAE_PASS"),
        trade="brokers.bbae:bbaeTrade",
        holdings="brokers.bbae:bbaeGetHoldings",
        validate="brokers.bbae:bbaeValidate",
        session_getter="brokers.bbae:get_bbae_session",
        notes="May require CAPTCHA/OTP",
    ),
    BrokerSpec(
        name="DSPAC",
        session_key="dspac",
        env_vars=("DSPAC_USER", "DSPAC_PASS"),
        trade="brokers.dspac:dspacTrade",
        holdings="brokers.dspac:dspacGetHoldings",
        validate="brokers.dspac:dspacValidate",
        session_getter="brokers.dspac:get_dspac_session",
        notes="May require CAPTCHA/OTP",
    ),
    BrokerSpec(
        name="SoFi",
        session_key="sofi",
        env_vars=("SOFI_USER", "SOFI_PASS"),
        trade="brokers.sofi:sofiTrade",
        holdings="brokers.sofi:sofiGetHoldings",
        validate="brokers.sofi:sofiValidate",
        session_getter="brokers.sofi:get_sofi_session",
        notes="Username/password",
    ),
    BrokerSpec(
        name="Webull",
        session_key="webull",
        env_vars=("WEBULL_ACCESS_TOKEN", "WEBULL_REFRESH_TOKEN", "WEBULL_UUID"),
        trade="brokers.webull:webullTrade",
        holdings="brokers.webull:webullGetHoldings",
        validate="brokers.webull:webullValidate",
        session_getter="brokers.webull:get_webull_session",
        notes="Pre-obtained credentials via Chrome extension",
    ),
    BrokerSpec(
        name="WellsFargo",
        session_key="wellsfargo",
        env_vars=("WELLSFARGO_USER", "WELLSFARGO_PASS"),
        trade="brokers.wellsfargo:wellsfargoTrade",
        holdings="brokers.wellsfargo:wellsfargoGetHoldings",
        session_getter="brokers.wellsfargo:get_wellsfargo_session",
        notes="Browser automation via Zendriver",
    ),
    BrokerSpec(
        name="Chase",
        session_key="chase",
        env_vars=("CHASE_USER", "CHASE_PASS"),
        trade="brokers.chase:chaseTrade",
        holdings="brokers.chase:chaseGetHoldings",
        session_getter="brokers.chase:get_chase_session",
        notes="Browser automation",
    ),
)

# Name -> BrokerSpec, in canonical order.
BROKERS: dict[str, BrokerSpec] = {spec.name: spec for spec in _SPECS}


# --- Queries (pure, import nothing) ---------------------------------------


def get(name: str) -> Optional[BrokerSpec]:
    """Return the spec for ``name`` (display name), or ``None`` if unknown."""
    return BROKERS.get(name)


def all_specs() -> list[BrokerSpec]:
    """All specs in canonical order, regardless of ``enabled``."""
    return list(BROKERS.values())


def all_names() -> list[str]:
    """All broker display names in canonical order."""
    return list(BROKERS.keys())


def enabled_specs() -> list[BrokerSpec]:
    """Specs with ``enabled=True``, in canonical order."""
    return [s for s in BROKERS.values() if s.enabled]


def enabled_names() -> list[str]:
    """Display names of enabled brokers, in canonical order."""
    return [s.name for s in BROKERS.values() if s.enabled]


# --- Lazy resolution (imports broker modules on demand) -------------------

TradeFn = Callable[[str, float, str, Optional[float]], Awaitable[Any]]
HoldingsFn = Callable[[Optional[str]], Awaitable[Any]]
ValidateFn = Callable[[str, float, str, Optional[float]], Awaitable[Any]]

_resolve_cache: dict[str, Callable[..., Any]] = {}


def _resolve(ref: str) -> Callable[..., Any]:
    """Resolve a ``"module:symbol"`` ref to its callable, caching the result.

    This is the only place broker modules get imported. A bad ref surfaces
    here (at first resolve) rather than at import time; the consistency test
    resolves every ref eagerly so CI catches typos.
    """
    fn = _resolve_cache.get(ref)
    if fn is None:
        module_path, _, symbol = ref.partition(":")
        if not module_path or not symbol:
            raise ValueError(f"malformed broker ref: {ref!r} (want 'module:symbol')")
        module = importlib.import_module(module_path)
        fn = getattr(module, symbol)
        _resolve_cache[ref] = fn
    return fn


def _spec_or_raise(name: str) -> BrokerSpec:
    spec = BROKERS.get(name)
    if spec is None:
        raise KeyError(f"unknown broker: {name!r}")
    return spec


def resolve_trade(name: str) -> TradeFn:
    """Resolve a broker's ``Trade`` function."""
    return _resolve(_spec_or_raise(name).trade)


def resolve_holdings(name: str) -> HoldingsFn:
    """Resolve a broker's ``GetHoldings`` function."""
    return _resolve(_spec_or_raise(name).holdings)


def resolve_validate(name: str) -> Optional[ValidateFn]:
    """Resolve a broker's ``Validate`` function, or ``None`` if it has none."""
    ref = _spec_or_raise(name).validate
    return _resolve(ref) if ref else None


def resolve_session_getter(name: str) -> Callable[..., Awaitable[Any]]:
    """Resolve a broker's ``get_<x>_session`` coroutine function."""
    return _resolve(_spec_or_raise(name).session_getter)


def get_broker_function(
    broker_name: str, function_type: str
) -> Optional[Callable[..., Any]]:
    """Resolve a broker's ``"trade"`` / ``"holdings"`` / ``"validate"`` function
    if the broker is enabled, else ``None``.

    Registry-backed replacement for the old ``tui.broker_functions``
    ``get_broker_function``. Returns ``None`` for an unknown or disabled broker,
    or for a ``"validate"`` request on a broker that has no validate function.
    """
    spec = BROKERS.get(broker_name)
    if spec is None or not spec.enabled:
        return None
    if function_type == "trade":
        return _resolve(spec.trade)
    if function_type == "holdings":
        return _resolve(spec.holdings)
    if function_type == "validate":
        return _resolve(spec.validate) if spec.validate else None
    return None


def broker_functions_map(
    enabled_only: bool = True,
) -> dict[str, dict[str, Callable[..., Any]]]:
    """Build the ``{name: {"trade", "holdings", "validate"?}}`` map the CLI and
    TUI consume (the shape the deleted ``tui.broker_functions.BROKER_CONFIG``
    provided). The ``"validate"`` key is present only for brokers that have a
    validate function. Resolving the refs imports those broker modules, so call
    this from code that is going to dispatch to brokers anyway — not at the top
    of a single-broker process.
    """
    specs = enabled_specs() if enabled_only else all_specs()
    out: dict[str, dict[str, Callable[..., Any]]] = {}
    for spec in specs:
        entry: dict[str, Callable[..., Any]] = {
            "trade": _resolve(spec.trade),
            "holdings": _resolve(spec.holdings),
        }
        if spec.validate:
            entry["validate"] = _resolve(spec.validate)
        out[spec.name] = entry
    return out
