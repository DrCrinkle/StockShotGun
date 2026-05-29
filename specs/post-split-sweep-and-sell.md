# Plan: Post-Reverse-Split Sweep & Sell

## Task Description

Add a `sweep` command to StockShotGun that checks selected brokers for post-reverse-split shares and sells them. After a reverse split, fractional shares from small pre-split positions round up to 1 whole post-split share. This feature automates the detection of those arrived shares and executes sell orders across all brokers where the post-split share is present.

**This plan is structured as two versions:**
- **v1 (Detection-only):** Check holdings across brokers, classify post-split state, report results. No sells.
- **v2 (Auto-sell):** Add account-targeted sell capability on top of v1. Requires broker API changes to support per-account order routing.

## Objective

**v1:** Users can run `python3 src/main.py sweep TICKER --ratio 1:25` and see a per-broker, per-account status report showing which brokers have received the post-split share, which are still processing, and which likely received cash-in-lieu.

**v2:** Users can add `--execute` to automatically sell arrived shares, with account-level targeting and double-sell prevention.

## Problem Statement

Reverse split arbitrage requires buying small quantities across many brokers, then selling the rounded-up post-split share. Currently, the user must manually check holdings across all 13 brokers to see which have received the post-split share, then manually submit sell orders. This is tedious, time-sensitive (selling quickly captures the best price), and error-prone. The core challenge is **detection**: distinguishing a post-split share from a pre-split holding, handling the variable processing window (3 days for self-clearing brokers up to 3+ weeks for Apex-cleared brokers since their Nov 2024 policy change), recognizing intermediate states like fractional share delivery (Robinhood pattern), and avoiding false sells.

## Solution Approach

### Detection Algorithm (First-Principles Design)

The split **ratio** is the discriminator. Without it, 1 pre-split share is indistinguishable from 1 post-split share. With the ratio, we can calculate the expected post-split quantity and compare:

```
expected_post_qty = ceil(pre_split_qty / ratio_denominator)
```

For typical arbitrage plays: `ceil(1 / 25) = 1` or `ceil(3 / 25) = 1`

**State machine per (ticker, broker, account) triplet:**

The state machine operates on two inputs: the `HoldingsOutcome` (what the holdings query returned) and the `observed_qty` (the quantity within a successful response). This separation addresses [Codex finding #4] — transport/auth failures are distinct from position classification.

```
                    ┌─────────────────┐
                    │ Holdings Query  │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───────┐ ┌───▼──────┐ ┌────▼─────────┐
     │ QUERY_SUCCESS  │ │ NO_CREDS │ │ QUERY_ERROR  │
     │ (got data)     │ │ (skip)   │ │ (auth/network│
     └────────┬───────┘ └──────────┘ │  /timeout)   │
              │                       └──────────────┘
              │ classify on observed_qty:
              │
     ┌────────┼────────────────────────────────┐
     │        │        │          │             │
  qty==0   qty==pre  0<qty<1   qty>=1 &&     qty==pre &&
  or None  && pre    (frac)    qty<=exp &&   pre==exp
  │        !=exp                qty<pre       │
  │        │        │          │             │
  ▼        ▼        ▼          ▼             ▼
PROCESSING AWAITING FRACTIONAL SHARE_       AMBIGUOUS
           _SPLIT   _PENDING   ARRIVED
```

**Key insight:** The only way `observed_qty` can be a small positive number that's less than `pre_split_qty` AND matches `expected_post_qty` is if the reverse split has settled and rounding occurred.

**Broker settlement patterns (from research):**

Not all brokers transition the same way. Three distinct patterns exist:

1. **Fractional-first brokers** (Robinhood, TastyTrade): Show the fractional amount (e.g., 0.04 shares) first, then round up to 1 whole share at a later date. The `FRACTIONAL_PENDING` state captures this intermediate step.
2. **Zero-then-appear brokers** (Apex-cleared: BBAE, DSPAC, Firstrade, Public, SoFi, Webull): Position disappears entirely (0 shares) for the processing window, then 1 whole share appears when Apex allocates. As of Nov 2024, Apex no longer pre-pays round-up shares — allocation takes **3+ weeks**.
3. **Trading-blocked brokers** (Fennel): Broker blocks all trading on the ticker until the post-split share is back. Holdings query may return data but sell orders will be rejected.

**Edge cases handled:**
- `pre_split_qty == 1` AND `expected_post_qty == 1`: Cannot distinguish by quantity alone. The `--force` flag resolves this. Flagged as `AMBIGUOUS`.
- Ticker symbol changes: Query both `TICKER` and `TICKERD` (FINRA temporary suffix)
- Float quantities: Some brokers return `"1.0"` or `"0.04"` — normalize to float before comparison
- TastyTrade fractional deposits: May be permanently unsellable through normal UI. Flag as `FRACTIONAL_PENDING` with a warning.
- Sell failures on blocked tickers (v2): Log the failure, don't treat as a fatal error — the share exists but isn't tradeable yet.

### Integration with Existing Infrastructure

- **Holdings queries**: Reuse existing `GetHoldings(ticker)` across all brokers
- **Sell execution (v2 only)**: Requires account-targeted sell — see v2 section
- **Session management**: Reuse `session_manager.initialize_selected_sessions()`
- **Rate limiting**: All holdings calls go through existing `rate_limiter`
- **Automation recap (v2)**: Cross-reference `buy_signals` table for ratio/qty context
- **CLI patterns**: Refactor to subcommand model (see CLI Refactor section)

## Relevant Files

### Existing Files to Modify

- **`src/main.py`** — Refactor argument parser to subcommand model; add `sweep` subcommand; implement `_run_sweep()` handler
- **`src/cli_runtime.py`** — Add `SWEEP_NO_SHARES_FOUND` exit code

### New Files to Create

- **`src/sweep.py`** — Core sweep logic: detection algorithm, state classification, result aggregation. Separated from main.py to keep the module focused and testable.

### v2-Only File Changes

- **`src/automation_recap.py`** — Add `sweep_state` table schema; add methods to query `buy_signals` for ratio/qty/broker context
- **`src/order_processor.py`** — Potentially extend to support account-targeted orders
- **`tui/broker_functions.py`** — May need account-targeted sell function entries
- **Broker modules** — May need per-account sell variants for brokers with multiple accounts

## Implementation Phases

### Phase 1: CLI Subcommand Refactor

Refactor `main.py`'s argument parser from a flat action grammar to proper subcommands. This is a prerequisite that also benefits the existing commands.

### Phase 2: v1 Detection Engine (`src/sweep.py`)

Build the core detection logic as a standalone module. Pure logic with no broker dependencies — takes holdings data as input and produces classification results.

### Phase 3: v1 CLI Command & Broker Integration

Wire the detection engine into the CLI as the `sweep` subcommand. Connect to existing holdings infrastructure. Handle session initialization and concurrent broker queries. Detection and reporting only — no sells.

### Phase 4: v2 Account-Targeted Sell & State Tracking (future)

Add account-targeted sell capability to broker interfaces. Add `sweep_state` table. Add `--execute` and `--from-db` modes. This phase is deferred until v1 is validated against real split events.

## Step by Step Tasks

IMPORTANT: Execute every step in order, top to bottom.

### 1. Refactor CLI to Subcommand Model (`src/main.py`)

The current parser uses a flat grammar with shared positionals (`action`, `quantity`, `ticker`, `price`). `sweep` doesn't fit this shape — it needs `ticker` and `--ratio` but not `quantity` or `price`. Rather than hacking around the flat parser, refactor to `argparse` subcommands.

- Create a top-level `ArgumentParser` with `subparsers`
- Migrate each existing action (`buy`, `sell`, `holdings`, `health`, `setup`, `automate`) to its own subparser with only the args it needs
- Preserve all existing flags (`--broker`, `--output`, `--request-id`, `--non-interactive`, `--dry-run`, `--mock-brokers`, `--log-format`, `--log-file`) as shared parent parser args
- Add `sweep` subparser with its own args (see step 3)
- Ensure `run_cli()` dispatch logic works with the new `args.subcommand` pattern
- **Critical:** All existing CLI invocations must continue to work identically. Run the full validation command suite before proceeding.

### 2. Create the Sweep Detection Module (`src/sweep.py`)

- Define `HoldingsOutcome` enum to separate query-level results from position classification [addresses Codex #4]:
  ```python
  class HoldingsOutcome(StrEnum):
      SUCCESS = "success"         # Got holdings data back
      NO_CREDENTIALS = "no_creds" # Broker not configured
      AUTH_FAILURE = "auth_fail"  # Session/login failed
      QUERY_ERROR = "query_error" # Network/timeout/unexpected error
  ```
- Define `SweepStatus` enum for position-level classification:
  ```python
  class SweepStatus(StrEnum):
      AWAITING_SPLIT = "awaiting_split"
      PROCESSING = "processing"
      FRACTIONAL_PENDING = "fractional_pending"
      SHARE_ARRIVED = "share_arrived"
      AMBIGUOUS = "ambiguous"
      SKIPPED = "skipped"       # NO_CREDENTIALS
      ERROR = "error"           # AUTH_FAILURE or QUERY_ERROR
  ```
  Note: `CIL_OR_TIMEOUT` and `BLOCKED` are removed from v1. They required inputs (effective date, sell-attempt results) that the v1 detection-only model doesn't have [addresses Codex #1]. They become v2 features when `sweep_state` tracks timestamps across runs.
- Define broker profile as separate flags instead of a single tier enum [addresses Codex #7]:
  ```python
  @dataclass(frozen=True)
  class BrokerSplitProfile:
      clearing: str                          # "apex", "self", "rqd", "unknown"
      processing_window_days: int            # Expected days before share appears
      fractional_intermediate: bool          # Shows fractional before rounding?
      round_up_expected: bool                # Historically rounds up?
      trade_may_be_blocked: bool             # Blocks trading during processing?
      cil_likely: bool                       # Typically pays cash-in-lieu?
      notes: str                             # Human-readable context

  BROKER_PROFILES: dict[str, BrokerSplitProfile] = {
      "Robinhood": BrokerSplitProfile("self", 15, True, True, True, False, "Delivers fractional first, rounds up later"),
      "TastyTrade": BrokerSplitProfile("self", 15, True, True, False, False, "Fractional may be permanently unsellable"),
      "BBAE": BrokerSplitProfile("apex", 25, False, True, False, False, "$0.25 round-up fee"),
      "DSPAC": BrokerSplitProfile("apex", 25, False, True, False, False, "Top RSA broker"),
      "Firstrade": BrokerSplitProfile("apex", 25, False, True, False, False, "Follows issuer instructions"),
      "Public": BrokerSplitProfile("apex", 25, False, True, False, False, "Reverse split fee applies"),
      "SoFi": BrokerSplitProfile("apex", 25, False, True, False, False, "Shows PRESPLIT activity"),
      "Webull": BrokerSplitProfile("apex", 25, False, True, False, False, "Omnibus with Apex, not self-clearing"),
      "Schwab": BrokerSplitProfile("self", 5, False, False, False, True, "CIL only — does not round up"),
      "WellsFargo": BrokerSplitProfile("self", 10, False, True, False, False, "Can't buy OTC under $1"),
      "Chase": BrokerSplitProfile("self", 10, False, False, False, True, "$5 OTC restriction, low RSA priority"),
      "Fennel": BrokerSplitProfile("unknown", 20, False, True, True, False, "Blocks trading until share arrives"),
      "Tradier": BrokerSplitProfile("rqd", 20, False, False, False, False, "$0.75 reorg fee"),
  }
  ```
- Define `SweepResult` dataclass:
  ```python
  @dataclass
  class SweepResult:
      broker: str
      account_id: str
      holdings_outcome: HoldingsOutcome
      status: SweepStatus
      observed_qty: float | None
      expected_post_qty: int
      pre_split_qty: int
      profile: BrokerSplitProfile
      details: str  # Human-readable explanation
  ```
- Implement `parse_ratio(ratio_str: str) -> tuple[int, int]`:
  - Parse "1:25" → `(1, 25)` (numerator, denominator)
  - Validate format, raise ValueError on bad input
- Implement `calculate_expected_post_qty(pre_split_qty: int, ratio_num: int, ratio_denom: int) -> int`:
  - Returns `math.ceil(pre_split_qty * ratio_num / ratio_denom)`
  - For typical arbitrage: `ceil(1 * 1 / 25) = 1`
- Implement `classify_holding(observed_qty: float | None, pre_split_qty: int, expected_post_qty: int) -> SweepStatus`:
  - This is a **pure function** on quantity only — no broker name, no timestamps [addresses Codex #1 — only emits states it can actually determine from quantity data]
  - `None` → `PROCESSING`
  - `observed_qty == 0` → `PROCESSING`
  - `observed_qty == pre_split_qty` AND `pre_split_qty == expected_post_qty` → `AMBIGUOUS`
  - `observed_qty == pre_split_qty` AND `pre_split_qty != expected_post_qty` → `AWAITING_SPLIT`
  - `0 < observed_qty < 1` (fractional) → `FRACTIONAL_PENDING`
  - `observed_qty >= 1` AND `observed_qty <= expected_post_qty` AND `observed_qty < pre_split_qty` → `SHARE_ARRIVED`
  - Everything else → `PROCESSING` (intermediate state)
- Implement `async def sweep_broker(broker_name: str, ticker: str, holdings_fn, pre_split_qty: int, ratio_str: str) -> list[SweepResult]`:
  - Call `holdings_fn(ticker)` wrapped in try/except to distinguish `None` return (no creds) from exceptions (query error) [addresses Codex #4]
  - If holdings function returns `None` → `HoldingsOutcome.NO_CREDENTIALS`, status `SKIPPED`
  - If exception → `HoldingsOutcome.QUERY_ERROR`, status `ERROR`, include traceback in details
  - If success but empty → `HoldingsOutcome.SUCCESS`, classify as `PROCESSING`
  - If success with data → `HoldingsOutcome.SUCCESS`, classify per account
  - Also try `holdings_fn(ticker + "D")` if primary returns empty (FINRA temporary suffix)
  - For each account in holdings, classify the holding
  - Return list of SweepResult (one per account, or one SKIPPED/ERROR per broker)
- Implement `async def sweep_all_brokers(ticker: str, ratio_str: str, pre_split_qty: int, broker_holdings: dict[str, callable], selected_brokers: list[str] | None = None) -> list[SweepResult]`:
  - Run `sweep_broker()` concurrently for all selected brokers via `asyncio.gather(return_exceptions=True)`
  - Collect and return all results
  - Handle per-broker exceptions without halting others

### 3. Add `sweep` CLI Subcommand (`src/main.py`)

- Add `sweep` subparser with these arguments:
  - `ticker` (positional, required) — the stock ticker to sweep
  - `--ratio` (required) — reverse split ratio, e.g., "1:25"
  - `--pre-qty` (optional, default=1) — number of shares purchased pre-split per broker
  - `--broker` (optional, repeatable) — limit to specific brokers
  - `--force` (optional flag) — include AMBIGUOUS results as sellable in output
- Shared parent parser args already available: `--output`, `--request-id`, `--mock-brokers`, `--non-interactive`, `--log-format`, `--log-file`
- Validate that `--ratio` matches the pattern `\d+:\d+`
- Route to `_run_sweep()` handler
- Note: `--execute`, `--from-db`, `--dry-run` are **v2 flags** — not added in v1

### 4. Implement `_run_sweep()` Handler — Detection Only (`src/main.py`)

- Follow the pattern established by `_run_automate_from_recap()`:
  1. Parse and validate arguments
  2. Determine broker selection (`--broker` flag or all configured brokers)
  3. Initialize sessions for selected brokers: `await session_manager.initialize_selected_sessions(selected_brokers)`
  4. Build `broker_holdings` dict mapping broker names to their holdings functions from `BROKER_FUNCTIONS`
  5. Call `sweep_all_brokers()` from `src/sweep.py`
  6. Display results per broker, per account:
     ```
     Sweep results for AREB (ratio 1:25, pre-split qty: 1):

       Robinhood    [account-1234]  SHARE_ARRIVED       qty=1     ✓ ready to sell
       Fennel       [acct-2345]     SHARE_ARRIVED       qty=1     ✓ ready to sell (note: may block trading)
       BBAE         [acct-3456]     PROCESSING          qty=0     ⏳ Apex-cleared — expect 3+ weeks
       Firstrade    [acct-4567]     PROCESSING          qty=0     ⏳ Apex-cleared — expect 3+ weeks
       TastyTrade   [acct-5678]     FRACTIONAL_PENDING  qty=0.04  ⚠ fractional delivered — round-up pending
       Schwab       [acct-6789]     PROCESSING          qty=0     ⏳ self-clearing, likely CIL
       Webull       [acct-7890]     PROCESSING          qty=0     ⏳ Apex-cleared — expect 3+ weeks
       Public       [acct-8901]     AMBIGUOUS           qty=1     ❓ can't distinguish pre/post (use --force)
       Tradier      [---]           SKIPPED             ---       ⊘ no credentials configured
       Chase        [---]           ERROR               ---       ✗ auth timeout after 600s

     Summary: 2 arrived, 5 processing, 1 fractional, 1 ambiguous, 1 skipped, 1 error
     ```
  7. v1 ends here — no sell execution. The output tells the user which brokers are ready, and they can use the existing `sell` command manually: `python3 src/main.py sell 1 AREB --broker Robinhood --broker Fennel`
  8. If `--output json`: wrap everything in `build_response_envelope()`

### 5. Add JSON Output Support for Sweep

- Follow the existing `build_response_envelope()` pattern
- Include in the response data:
  ```python
  {
      "ticker": ticker,
      "ratio": ratio_str,
      "pre_split_qty": pre_split_qty,
      "results": [
          {
              "broker": "Robinhood",
              "account_id": "1234",
              "holdings_outcome": "success",
              "status": "SHARE_ARRIVED",
              "observed_qty": 1,
              "expected_post_qty": 1,
              "profile": {"clearing": "self", "processing_window_days": 15, ...},
              "details": "ready to sell"
          },
          ...
      ],
      "summary": {
          "total_brokers_checked": 13,
          "share_arrived": 2,
          "processing": 5,
          "fractional_pending": 1,
          "awaiting_split": 0,
          "ambiguous": 1,
          "skipped": 1,
          "error": 1
      }
  }
  ```

### 6. Handle the "D" Suffix Ticker Variant

- In `sweep_broker()`, if primary ticker returns no holdings:
  - Try `ticker + "D"` (FINRA temporary corporate action suffix)
  - If found, use those results but note in `details` field: "Found under temporary symbol {ticker}D"
- Only try the "D" variant if the primary query returned zero positions across all accounts
- This handles the ~20 trading day window where FINRA appends the suffix

### 7. Validate v1 Implementation

- Run `python3 -m py_compile src/sweep.py src/main.py` to verify syntax
- Run `mypy src/sweep.py src/main.py --show-error-codes --pretty --ignore-missing-imports`
- Test with `--mock-brokers` flag:
  ```bash
  # Basic sweep
  python3 src/main.py sweep AREB --ratio 1:25

  # With mock: deterministic test data
  python3 src/main.py sweep AREB --ratio 1:25 --mock-brokers

  # JSON output
  python3 src/main.py sweep AREB --ratio 1:25 --mock-brokers --output json

  # Single broker
  python3 src/main.py sweep AREB --ratio 1:25 --broker Fennel

  # Force include ambiguous
  python3 src/main.py sweep AREB --ratio 1:25 --force
  ```
- Verify existing commands still work after subcommand refactor:
  ```bash
  python3 src/main.py buy 1 AAPL --dry-run --mock-brokers
  python3 src/main.py sell 1 AAPL --dry-run --mock-brokers
  python3 src/main.py holdings AAPL --broker Fennel
  python3 src/main.py health
  ```

### 8. v2 — Account-Targeted Sell (Future, Deferred)

This section documents what's needed for v2 but is NOT implemented in v1. [Addresses Codex #2, #3, #6, #8]

**Problem:** Current trade functions are broker-wide, not account-targeted. If one login has 3 accounts (brokerage + IRA + Roth) and only the IRA received the round-up share, the existing sell pipeline can't route to just that account.

**Required changes for v2:**
- **Broker API extension**: Add optional `account_id` parameter to trade functions. Brokers that support multi-account already discover account IDs during session init — they just need to accept a filter.
- **`tui/broker_functions.py`**: Add `targeted_sell` entry alongside existing `trade` function, or extend `trade` to accept account ID.
- **`sweep_state` table**: Add `split_date` or `signal_id` to uniqueness constraint [addresses Codex #6]:
  ```sql
  CREATE TABLE IF NOT EXISTS sweep_state (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ticker TEXT NOT NULL,
      split_signal_id INTEGER,       -- FK to buy_signals.id
      split_ratio TEXT NOT NULL,      -- e.g., "1:25"
      broker TEXT NOT NULL,
      account_id TEXT,
      status TEXT NOT NULL,
      observed_qty REAL,
      expected_post_qty INTEGER,
      first_checked TEXT NOT NULL,    -- when we first saw this state
      last_checked TEXT NOT NULL,
      sold_at TEXT,
      UNIQUE(ticker, split_ratio, broker, account_id)
  );
  ```
- **`buy_signals` schema extension**: Track which brokers the buy actually succeeded on, filled qty per account, and effective split date [addresses Codex #3].
- **`--execute` flag**: Submits sell orders only for `SHARE_ARRIVED` accounts (plus `AMBIGUOUS` with `--force`). Uses account-targeted sell.
- **`--from-db` flag**: Reads pending splits from `buy_signals` table with per-broker/account provenance.
- **`CIL_OR_TIMEOUT` state**: Enabled once `sweep_state` tracks `first_checked` timestamp — can compute business days elapsed since first observed as PROCESSING.
- **Double-sell prevention**: Check `sweep_state.sold_at IS NOT NULL` for the `(ticker, split_ratio, broker, account_id)` tuple before selling.

## Testing Strategy

### Unit Tests (`tests/test_sweep.py`)

Test the pure detection logic in isolation:

- `test_parse_ratio`: Valid ratios ("1:25", "1:10", "3:1"), invalid inputs ("abc", "1:", "")
- `test_calculate_expected_post_qty`: Various ratio/qty combos, edge cases (pre_qty=0, large ratios)
- `test_classify_holding_share_arrived`: qty=1 when pre=3 and ratio=1:25 → SHARE_ARRIVED
- `test_classify_holding_processing`: qty=0 → PROCESSING
- `test_classify_holding_processing_none`: qty=None → PROCESSING
- `test_classify_holding_awaiting`: qty=3 when pre=3 and ratio=1:25 → AWAITING_SPLIT
- `test_classify_holding_ambiguous`: qty=1 when pre=1 and expected=1 → AMBIGUOUS
- `test_classify_holding_fractional_pending`: qty=0.04 when pre=1 and ratio=1:25 → FRACTIONAL_PENDING
- `test_float_normalization`: qty="1.0" and qty=1.0 both produce correct classification
- `test_broker_profile_lookup`: All 13 brokers have a profile entry
- `test_broker_profile_fields`: Apex brokers have `processing_window_days=25`, self-clearing have shorter

### Integration Tests

- `test_sweep_broker_no_creds`: holdings_fn returns None → SKIPPED with NO_CREDENTIALS
- `test_sweep_broker_auth_failure`: holdings_fn raises exception → ERROR with QUERY_ERROR
- `test_sweep_broker_empty_holdings`: holdings_fn returns `{}` → PROCESSING
- `test_sweep_broker_multi_account`: holdings_fn returns 3 accounts with different qtys → 3 SweepResults
- `test_sweep_all_brokers_concurrent`: Mock 5 brokers, verify all run concurrently
- `test_d_suffix_fallback`: Primary returns empty, ticker+"D" returns data → uses D results
- Test CLI argument parsing for all flag combinations
- Test JSON output format matches expected schema

### Manual E2E Verification

- Run against real brokers with a known post-split position
- Verify detection correctly identifies arrived shares vs processing vs errors
- Confirm `--force` includes ambiguous results in the "ready to sell" list

Note: This project has no `tests/` directory yet. The first test file creates the scaffolding [acknowledges Codex #9].

## Acceptance Criteria

### v1 (Detection-Only)

- [ ] CLI refactored to subcommand model; all existing commands work identically
- [ ] `python3 src/main.py sweep TICKER --ratio 1:25` checks all configured brokers and reports per-broker, per-account status
- [ ] `--broker` flag limits sweep to specified brokers
- [ ] `--force` flag includes AMBIGUOUS results as "ready to sell" in output
- [ ] `--output json` produces valid JSON envelope with all sweep data
- [ ] Detection classifies: SHARE_ARRIVED, PROCESSING, FRACTIONAL_PENDING, AWAITING_SPLIT, AMBIGUOUS, SKIPPED, ERROR
- [ ] `HoldingsOutcome` distinguishes no-creds, auth failure, query error from empty holdings
- [ ] FRACTIONAL_PENDING correctly detected for sub-1.0 quantities
- [ ] All 13 brokers have a `BrokerSplitProfile` entry with accurate metadata
- [ ] Profile context shown in status output (e.g., "Apex-cleared — expect 3+ weeks")
- [ ] Ticker "D" suffix variant checked when primary returns empty
- [ ] Existing CLI commands (buy, sell, holdings, automate, health, setup) unaffected by subcommand refactor
- [ ] Code compiles: `python3 -m py_compile src/sweep.py src/main.py`
- [ ] Type checks: `mypy src/sweep.py --ignore-missing-imports` passes

### v2 (Auto-Sell, Deferred)

- [ ] `--execute` flag submits sell orders for SHARE_ARRIVED accounts
- [ ] Sells are account-targeted, not broker-wide
- [ ] `sweep_state` table tracks state per (ticker, split_ratio, broker, account_id)
- [ ] Double-sell prevention via `sold_at` check
- [ ] `--from-db` reads pending splits from enriched `buy_signals` table

## Validation Commands

Execute these commands to validate v1 is complete:

- `python3 -m py_compile src/sweep.py src/main.py` — Verify syntax and imports
- `mypy src/sweep.py src/main.py --show-error-codes --pretty --ignore-missing-imports` — Type checking
- `python3 src/main.py sweep --help` — Verify subcommand registered correctly
- `python3 src/main.py sweep AREB --ratio 1:25 --mock-brokers` — E2E detection with mock data
- `python3 src/main.py sweep AREB --ratio 1:25 --mock-brokers --output json` — JSON output validation
- `python3 src/main.py buy 1 AAPL --dry-run --mock-brokers` — Verify buy still works after refactor
- `python3 src/main.py sell 1 AAPL --dry-run --mock-brokers` — Verify sell still works
- `python3 src/main.py holdings AAPL --broker Fennel` — Verify holdings still works
- `python3 src/main.py health` — Verify health still works

## Notes

### Codex Review Findings — How Each Is Addressed

| # | Finding | Severity | Resolution |
|---|---------|----------|------------|
| 1 | `CIL_OR_TIMEOUT` and `BLOCKED` unimplementable without timestamp/rejection data | High | Removed from v1. Become v2 features via `sweep_state` timestamp tracking. |
| 2 | Per-account detection doesn't match broker-wide sell pipeline | High | v1 is detection-only. v2 deferred until account-targeted sell is built. |
| 3 | `--from-db` under-specified; `buy_signals` lacks per-broker provenance | High | Deferred to v2 with schema enrichment requirements documented. |
| 4 | `None`/missing conflates no-creds, auth failure, and empty holdings | High | Added `HoldingsOutcome` enum; `sweep_broker()` separates transport from classification. |
| 5 | CLI assumes subcommand model that doesn't exist | Medium | Step 1 refactors to subcommand model. User confirmed this is desired. |
| 6 | `sweep_state` dedupe key breaks for repeat splits | Medium | v2 key includes `split_ratio` and `split_signal_id`. |
| 7 | `BrokerSettlementTier` mixes independent dimensions | Medium | Replaced with `BrokerSplitProfile` dataclass with separate flags. |
| 8 | Account-targeted sell requires broker API changes | Medium | Documented as v2 prerequisite. v1 avoids the issue entirely. |
| 9 | No test infrastructure exists | Low | Noted; first test creates `tests/` scaffolding. |

### Reverse Split Settlement Timeline (from research)

Timelines vary dramatically by clearing firm:

| Clearing Model | Share Appearance | CIL Payment | Example Brokers |
|----------------|-----------------|-------------|-----------------|
| **Apex-cleared** | **3+ weeks** (since Nov 2024 policy change — Apex no longer pre-pays) | 2-3 weeks | BBAE, DSPAC, Firstrade, Public, SoFi, Webull |
| **Self-clearing** | 1-2 business days | 2-3 business days | Schwab (CIL only), Wells Fargo, Chase |
| **Fractional-first** | Fractional: days. Round-up: 1-3 weeks | N/A | Robinhood, TastyTrade |
| **Trading-blocked** | Unknown (blocked until resolved) | N/A | Fennel |

**Critical Nov 2024 change:** Apex Clearing stopped pre-paying round-up shares. This means 6 of 13 brokers now have a **3+ week processing window** where holdings show 0 shares. Before this change, many of these brokers offered "next-day selling."

**Implication**: Users should run `sweep` starting ~3 business days after the effective date for self-clearing brokers, and continue re-running periodically for **up to 4 weeks** to catch Apex-cleared brokers as they resolve.

### Broker-Specific Considerations (from research)

**Fractional-First Brokers:**
- **Robinhood**: Delivers fractional share first (e.g., 0.04), then rounds up to 1 whole share at a later date. Temporarily prevents trading during corporate action processing. A sell failure during the blocked window should be logged, not treated as a fatal error.
- **TastyTrade**: Known issue — deposits exact fractional qty (e.g., 0.04 shares) that is **permanently unsellable** through normal UI. The UI only handles whole-share orders. If sweep detects a fractional qty on TastyTrade, flag it with a warning: "TastyTrade fractional deposit — may require manual resolution."

**Apex-Cleared Brokers (3+ week delay since Nov 2024):**
- **BBAE**: Charges $0.25 per round-up event (since Jan 2025). Position shows 0 during processing. Fee is automatic — no code change needed.
- **DSPAC**: Listed as top RSA broker. Same Apex delay applies. Limited firsthand data on processing behavior.
- **Firstrade**: Follows issuer's instructions (round-up if company specifies, CIL otherwise). Open orders auto-cancelled on split effective date.
- **Public**: Charges a reverse split fee (waived for Premium). Previously offered "next-day selling" — this likely no longer holds post-Apex policy change.
- **SoFi**: Shows "PRESPLIT" activity in account during processing. Has a unique proprietary account model for fractional shares that may partially insulate from Apex delays.
- **Webull**: Still Apex-cleared (NOT self-clearing). Moved to omnibus clearing model (Webull holds cash, Apex holds securities) but this doesn't eliminate the 3+ week delay. Official policy is CIL, but round-ups have historically occurred. 100-share minimum for penny stocks. revRSS author dropped Webull from active RSA use in Nov 2024.

**Self-Clearing Brokers:**
- **Schwab**: Self-clearing. **Does NOT round up** — pays cash-in-lieu for fractional remainder. Whole shares appear quickly (same day/next day), CIL cash credit ~2 business days later. Not useful for RSA round-up profit.
- **Wells Fargo**: Self-clearing (Wells Fargo Clearing Services). No definitive data on round-up vs CIL — follows issuer's terms. Valued by RSA community for multiple account types (brokerage + multiple IRAs from single login). **Cannot buy OTC stocks under $1.00** and charges $34.95 fee for penny stocks — limits RSA opportunities. No API (browser automation only). No reorg fee.
- **Chase**: Self-clearing (J.P. Morgan Securities). **$5 OTC penny stock purchase restriction** blocks most RSA targets. Zero community evidence of successful round-ups. Not on any RSA broker recommendation list. Low-priority for RSA.

**Trading-Blocked Brokers:**
- **Fennel**: Blocks all trading on the ticker until the post-split share is back in the account. Charges $0.30 reverse split fee. Clean from an API perspective — the error is explicit.

**Unknown/Limited Data:**
- **Tradier**: Uses RQD Clearing (not Apex). $0.75 reorg fee + $0.35/trade makes it expensive for RSA. Limited user reports on split behavior.

**Browser-based brokers** (Wells Fargo, Chase, SoFi): Use 600-second timeout for holdings queries. These are slower but the existing timeout infrastructure handles it.

### Apex Clearing Policy Change (Nov 18, 2024) — Critical Context

As of November 18, 2024, Apex Clearing no longer pre-pays shares for reverse stock split allocations. Shares are only allocated to customer accounts **after Apex receives them from the issuer**, which takes 3+ weeks. During this window, customers who would receive only a round-up share see **no shares** in their accounts. If they attempt to sell during this window, it is treated as a **short sale**.

This affects 6 of 13 StockShotGun brokers: BBAE, DSPAC, Firstrade, Public, SoFi, Webull.

### Webull Clearing Clarification

Webull is **NOT self-clearing** despite some community assumptions. As of Dec 2024 SEC filings, Apex Clearing Corporation remains their clearing firm. However, Webull transitioned many accounts from a **fully disclosed** model to an **omnibus** model — Webull holds customer cash while Apex holds securities. This gives Webull more intermediary control but does NOT eliminate the Apex 3+ week processing delay. The omnibus clearing agreement was originally signed Sep 2017 with amendments in 2021 and 2022.

### Wells Fargo RSA Viability

Wells Fargo is self-clearing and valued by the RSA community for multiple account types (brokerage + Traditional IRA + Roth IRA + SEP IRA from one login). However, it has significant limitations:
- **Cannot buy OTC stocks under $1.00** (sales only)
- **$34.95 fee** for penny stock trades (stocks under $1)
- No public API — browser automation only
- No definitive data on round-up vs CIL behavior
These constraints mean Wells Fargo works for exchange-listed reverse split targets above $1 but is unsuitable for sub-$1 OTC plays.

### Chase RSA Viability

Chase (J.P. Morgan Self-Directed Investing) is self-clearing but has a **$5 OTC penny stock purchase restriction** that blocks most RSA targets. Zero community evidence of successful round-ups exists, and Chase appears on no RSA broker recommendation lists. Chase should be treated as low-priority for sweep operations.

### The Ratio is Required

Unlike buy/sell which are simple order commands, sweep **requires the ratio** to function correctly. Without it, we cannot distinguish pre-split holdings from post-split holdings. The ratio comes from either:
1. The `--ratio` CLI flag (primary, v1)
2. The `buy_signals.ratio` field in the automation database (via `--from-db`, v2)

If neither is provided, the command should error with a clear message explaining why the ratio is needed.

### Lessons from auto-rsa (NelsonDane/auto-rsa)

auto-rsa is the closest open-source reference implementation for multi-broker RSA trading (16 brokers, Discord bot + CLI). Key findings from a deep architecture study:

**What auto-rsa does NOT have (our whitespace):**
- No sweep/detection logic — zero intelligence about post-split share arrival
- No holdings snapshot storage or split-aware comparison
- No polling loop for monitoring split settlement
- No split calendar API integration
- No CIL detection
- This means StockShotGun's sweep feature would be net-new in open source

**Patterns adopted:**
1. **Dry-run defaults to true** — v1 is detection-only (inherently safe). v2's `--execute` flag requires explicit opt-in.
2. **Three-level holdings hierarchy** — auto-rsa's `parent → account → {stock: {qty, price, total}}` matches StockShotGun's holdings structure. Sweep tracks per (broker, account_id).
3. **Broker grouping** — Replaced auto-rsa's `day1`/`fast`/`most` with `BrokerSplitProfile` for more granular control.

**Patterns avoided:**
- Sequential broker execution — StockShotGun's `asyncio.gather()` is correct for sweep
- Match/case dispatch — StockShotGun's BROKER_FUNCTIONS dict is better
- Synchronous-only architecture — StockShotGun's async-first design is correct

### Future Enhancements (Out of Scope)

- **Automated polling/scheduling**: Run sweep on a cron via systemd timer or the `/schedule` skill
- **Split calendar API integration**: Automatically detect upcoming reverse splits via Financial Modeling Prep's Stock Split Calendar API (`/api/v3/stock_split_calendar`, ~$15/month) or Alpaca Corporate Actions API
- **TUI integration**: Add sweep as a TUI screen alongside holdings view
- **Cost basis tracking**: Calculate P&L on the round-up share vs purchase cost
- **SIFMA compliance monitoring**: Track whether specific brokers/tickers are switching to CIL-only
- **CIL detection**: Monitor cash balance changes to detect when a broker paid cash-in-lieu instead of rounding up
- **Discord bot integration**: Add sweep as a Discord command for unattended operation with OTP bridge (auto-rsa pattern)
- **Pre-split baseline snapshots**: Persist holdings state before split effective date for delta comparison
