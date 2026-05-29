# ADR 0001: Per-Account Position Granularity

- **Status**: Accepted
- **Date**: 2026-04-30

## Context

StockShotGun coordinates trades across many brokers simultaneously. For reverse
split arbitrage (RSA), we need to track each holding from buy through split
processing through sell. The question is the granularity of a stored Position:

- **Per-broker**: one row per broker per Trade — collapse all of a broker's accounts
  into a single position record.
- **Per-account**: one row per `(broker, account_id)` per Trade — track each
  brokerage account independently.

Several brokers expose multiple accounts under one set of credentials:

- **Webull**: supports comma-separated `WEBULL_ACCOUNT_ID` for multiple accounts.
- **Schwab**: OAuth account discovery; one user can have several accounts.
- **WellsFargo**: auto-discovers WELLSTRADE + IRA accounts from a single login.

The existing sweep code (`src/sweep.py`) already returns per-account results
(`SweepResult.account_id`), so internal data flow is already at account
granularity — the question is whether persistence matches.

## Decision

Persist Positions at **per-account** granularity. Each row is keyed by
`(trade_id, broker, account_id)`.

When buying into a Trade, all accounts at each enabled broker are included; each
becomes a separate Position row.

## Alternatives Considered

### Per-broker (rejected)

Simpler schema. But:

- Forces aggregating multi-account data, losing the structure already present in
  `SweepResult`.
- A taxable account and an IRA at the same broker can behave differently during
  a split (e.g., different settlement timing, different round-up policies).
- Crowd-sourced **Stocks Back** observations from the RSA forum are
  account-grained in spirit ("Tasty rounded my IRA but not my taxable") — modeling
  per-broker would lose that signal forever.
- Reversing later requires a data migration that splits historical aggregates.

### Per-position-lot (rejected)

Track every individual lot (separate buy events) within an account. Overkill — RSA
trades are typically a single buy per account, and splits process at the account
level regardless of lot.

## Consequences

**Positive**:

- Sweep history is naturally per-account; no aggregation loss.
- Multi-account brokers (Webull, Schwab, WellsFargo) get correct treatment without
  special cases.
- Future calibration of `BROKER_PROFILES.processing_window_days` from real Stocks
  Back observations remains feasible at the granularity the data is reported in.

**Negative**:

- More rows in the database. For 13 brokers × ~3 accounts each per Trade, a single
  RSA Trade may produce 30-40 Position rows. This is fine at expected scale (tens
  of Trades per month).
- Account discovery must run before Positions can be created — the buy flow needs
  to enumerate accounts per broker, not just count "this broker submitted N orders."

**Neutral**:

- Sweep code already at this granularity; no change needed.

## Reversibility

Reversible by aggregating per-account rows up to per-broker via a migration.
Forward direction (per-broker → per-account) would be lossier — historical Trades
would have no way to recover account-level data.
