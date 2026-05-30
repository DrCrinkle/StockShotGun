# ADR 0004: Broker Registry as Single Source of Truth — Lazy-Ref Registry + Generic Entrypoint

- **Status**: Accepted
- **Date**: 2026-05-30
- **Amends**: ADR 0003 (replaces its per-broker `python -m agentic.brokers.<name>`
  entrypoint contract and its hand-written per-broker `SPEC` modules; the
  per-broker process-isolation *intent* of 0003 is retained and, for the first
  time, actually enforced)

## Context

"What brokers exist and what each one needs" was restated across five places
that could silently disagree:

1. `brokers.base.BrokerConfig.BROKERS` — `session_key`, `env_vars`,
   `requires_mfa`, `enabled` (the de-facto canonical metadata).
2. `brokers/__init__.py` `__all__` — the trade/holdings/validate **symbol names**.
3. `brokers.session_manager.BROKER_MODULES` — the `get_<x>_session` getter per
   broker, plus 13 hard `from . import <broker>` imports.
4. `tui.broker_functions.BROKER_CONFIG` — direct function refs (validate
   missing for 4 brokers; entries went orphaned after the router migration).
5. The 13 `agentic/brokers/<name>/__init__.py` `SPEC` modules +
   `agentic.router._server.ALL_BROKER_SUBPACKAGES` — `supports_fractional`,
   `notes`, `list_accounts_fn`, and the load order.

Adding one broker was a ~9-file edit; nothing could assert the five registries
agreed, because there was no single thing to test.

Two findings shaped the decision:

- **The duplication exists to dodge a circular import.** Broker modules import
  *from* `base.py` (`rate_limiter`, `http_client`, `BrokerConfig`), so
  `BrokerConfig` cannot hold references to broker functions
  (`base.py → robinhood.py → base.py`). Each consumer re-imports the functions
  itself to avoid the cycle.
- **ADR 0003's per-broker isolation was an import-level fiction.**
  `brokers/__init__.py` eagerly imports all 13 broker modules, and every
  `SPEC` does `from brokers import <fn>`. So `python -m agentic.brokers.<name>`
  already imported all thirteen brokers and their SDKs. The credential scoping
  (env vars per process) held; code-level blast-radius isolation did not.

A blast-radius check confirmed the only consumers of the per-broker *function
symbols* were the 13 `SPEC` files and `tui/broker_functions.py` — both removed
here. Every other consumer imports only `session_manager` and `BrokerConfig`.
De-eager-ing `brokers/__init__.py` is therefore safe.

## Decision

Introduce **`src/brokers/registry.py`** as the single source of truth.

### Pure-data `BrokerSpec`

A frozen dataclass per broker, carrying **lazy** references — never live
function objects:

- Metadata: `name`, `session_key`, `env_vars`, `requires_mfa`, `enabled`,
  `supports_fractional`, `notes`.
- Lazy refs as `"module:symbol"` strings: `trade`, `holdings`,
  `validate` (optional), `session_getter`.
- `multi_account: bool` — a flag, **not** a function ref, so the registry
  imports no agentic internals.

A registry-owned resolver performs `importlib.import_module` + `getattr` with
caching. **Importing `brokers.registry` imports zero broker SDKs.**

### Everyone derives

- `BrokerConfig` becomes a thin facade over the registry; its public interface
  (`get_broker_info`, `get_session_key`, enabled queries) is preserved so
  `main.py` and `cli/*` are untouched.
- `session_manager` drops `BROKER_MODULES` and its 13 hard imports;
  `get_session` resolves the session getter via the registry's lazy ref.
- `tui/broker_functions.py` is **deleted**; the TUI derives trade/holdings from
  the registry.
- The 13 `SPEC` modules, the 13 `agentic/brokers/<name>/` packages, and
  `ALL_BROKER_SUBPACKAGES` are **removed**. The router builds the runtime
  `BrokerMCPSpec` (resolved callables + MCP flags; `multi_account` mapped to the
  `session_manager_accounts` closure) from each enabled `BrokerSpec`.

### Generic entrypoint (replaces ADR 0003's per-broker module path)

`python -m agentic.broker <name>` resolves **only** the named broker from the
registry and runs its MCP server. This supersedes
`python -m agentic.brokers.<name>` and — because the registry is lazy — finally
makes per-broker process isolation real: a single-broker process imports one
broker, not thirteen.

### Type split

`brokers.registry.BrokerSpec` is pure data and MCP-agnostic. The agentic layer
keeps `BrokerMCPSpec` as the resolved runtime view built from a `BrokerSpec`.
The registry never depends on `agentic.*`.

### The registry sits behind the gate

The registry resolves broker callables, but only the gated `BrokerMCPServer`
may invoke them. The existing enforcement guard
(`tests/agentic/test_cli.py::test_static_no_direct_broker_sdk_calls_in_cli`)
remains authoritative; the registry must not become a path around the gate.

## Alternatives Considered

- **Eager refs in a new module (unify-only).** A `registry.py` above the broker
  modules holding live function refs. Simplest, single source of truth, but
  bakes in "import one broker = import all thirteen" and leaves ADR 0003's
  isolation claim unmet. Rejected in favour of the lazy variant, which costs
  modest `importlib` machinery and delivers real isolation.
- **Extend `BrokerConfig.BROKERS` in place with function refs.** Reintroduces
  the circular import the current structure exists to dodge. Rejected.
- **Keep 13 thin `__main__` shims** instead of one generic entrypoint.
  Preserves ADR 0003's documented path but keeps 13 files for what is one
  parameterized concern. Rejected for the generic entrypoint; this ADR records
  the contract change.

## Consequences

**Positive**

- Adding a broker is a single `BrokerSpec` entry; all sites derive.
- Three tests become possible that could not exist before: (1) **consistency** —
  every spec's lazy refs resolve to callables and the enabled set is coherent;
  (2) **isolation** — `import brokers.registry` leaves `sys.modules` free of any
  broker SDK; (3) **single-add** — one registry entry is sufficient for the
  router to expose the broker.
- `enabled` means one thing everywhere (previously honored by
  `get_broker_function` but ignored by `ALL_BROKER_SUBPACKAGES`).
- ADR 0003's isolation intent is enforced, not merely asserted.

**Negative**

- `brokers/__init__.py` stops re-exporting per-broker function symbols — a
  breaking change for any external caller relying on `from brokers import
  <broker>Trade`. Internal blast radius is contained to files removed here.
- `importlib`-based resolution defers some failures (a bad symbol name surfaces
  at first resolve, not at import). Mitigated by the consistency test, which
  resolves every ref eagerly in CI.
- Documentation referencing `python -m agentic.brokers.<name>` must be updated
  to the generic entrypoint.

## Reversibility

Reversible. The lazy registry can be re-expanded to eager per-consumer imports,
and per-broker entrypoints reconstituted from registry data, without data
migration — this is a code-structure change with no persisted-state impact.
