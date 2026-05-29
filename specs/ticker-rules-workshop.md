# Ticker Rules Workshop Draft

## Goal

Add per-ticker automation controls so recap-driven actions are configurable without changing code.

## Proposed Scope

- Per-ticker buy quantity override
- Per-ticker sell behavior override (holdings-based vs fixed qty)
- Per-ticker broker allowlists for buy and sell
- Per-ticker enable/disable and side-specific gating

## Draft Schema

```sql
CREATE TABLE IF NOT EXISTS ticker_rules (
  ticker TEXT PRIMARY KEY,
  enabled INTEGER NOT NULL DEFAULT 1,
  buy_qty INTEGER,
  sell_qty_mode TEXT NOT NULL DEFAULT 'holdings',
  sell_qty_fixed INTEGER,
  buy_brokers_json TEXT,
  sell_brokers_json TEXT,
  allow_buy INTEGER NOT NULL DEFAULT 1,
  allow_sell INTEGER NOT NULL DEFAULT 1,
  notes TEXT,
  updated_at TEXT NOT NULL
);
```

## Behavior Precedence

1. `ticker_rules` values when present
2. CLI overrides (`--broker`, `--default-qty`)
3. Existing automation defaults

## Expected Runtime Semantics

- If `enabled = 0`, skip all automation for ticker
- If `allow_buy = 0`, skip buy generation for ticker
- If `allow_sell = 0`, skip sell generation for ticker
- If `buy_qty` is set, use it instead of global default
- If `sell_qty_mode = 'holdings'`, derive live sell qty from holdings
- If `sell_qty_mode = 'fixed'`, use `sell_qty_fixed` (must be > 0)
- If broker JSON fields exist, intersect with currently available/selected brokers

## Validation Rules

- `ticker` must normalize to uppercase
- `buy_qty` and `sell_qty_fixed` must be positive integers when set
- `sell_qty_mode` must be `holdings` or `fixed`
- `buy_brokers_json` and `sell_brokers_json` must decode to arrays of valid broker names

## Auditability

Each generated order should include rule context in automation JSON output:

- `rule_applied: true|false`
- `rule_fields_applied: [...]`
- `rule_source: ticker_rules`

## Open Questions (Needs Workshop)

- Should CLI `--broker` hard-override ticker broker lists, or intersect with them?
- For `sell_qty_mode = fixed`, should fixed qty cap at holdings automatically?
- Should rule updates be managed by SQL only, or via new CLI (`automate-rules`)?
- Do we need effective date windows per rule?

## Suggested Incremental Rollout

1. Add table + read-only application in automate flow
2. Add strict validation and error reporting for malformed rules
3. Add minimal CLI management (`automate-rules list|get|set|delete`)
4. Add optional effective-date windows if needed

## Out of Scope (for first pass)

- Multi-profile strategy layers
- Rule inheritance across ticker groups
- External UI for rule management
