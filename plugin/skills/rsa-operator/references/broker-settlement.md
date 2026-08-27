# Broker settlement tiers for reverse splits

Clearing behaviour, not numbers. The live numeric windows are `BROKER_PROFILES` in
`src/sweep.py`; read that file when precision matters rather than trusting values copied
into prose, which drift.

The point of this document is to answer one question during a sweep: **is this position
late, or is it inside its own tier's normal window?** Judging every broker against the
fastest one produces false alarms and pointless intervention.

## Tiers

**Apex-cleared** — BBAE, DSPAC, Firstrade, Public, SoFi, Webull.

Since the November 2024 Apex policy change, reorganisation processing runs 3+ weeks.
Holdings legitimately read 0 shares for that entire period. This is the single most
impactful variable in sweep timing, and the most common cause of a position that looks
lost but is not. Note that **Webull is Apex via an omnibus model, not self-clearing**;
assuming otherwise is a recurring mistake.

**Fractional-first** — Robinhood, TastyTrade.

A fractional share is delivered first, and any round-up follows later. A sweep that sees a
fraction has not seen the end state. TastyTrade fractionals may be permanently unsellable,
so a fraction there can be a dead position rather than an interim one.

**Self-clearing** — Schwab, Wells Fargo, Chase.

Resolution is fast, in days rather than weeks, so a stalled position here is genuinely
stalled. Constraints:

- **Schwab processes cash in lieu only.** No round-up reaches the account, which makes
  Schwab legs cost without upside on an RSA play regardless of the issuer's terms. This
  feeds the per-broker arithmetic in `references/fractional-treatment.md` stage 3.
- **Wells Fargo** follows issuer terms but cannot buy OTC positions under $1.
- **Chase** restricts OTC under $5.

**Trading-blocked** — Fennel.

Blocks trading on the position until the post-split share arrives. A sell attempt before
arrival fails by design, not by error. Fennel is also the only broker with real
account-scoped dispatch (`multi_account=True`, `account_scoped_trade=True`), so each of
its accounts is a separate gated leg.

**Unknown** — Tradier.

RQD Clearing, with expensive reorganisation fees. Behaviour is not characterised; treat
outcomes here as unproven and watch the fee side.

## Using this during a sweep

1. Group the trade's positions by tier, not by ticker.
2. For each position, compare `resolved_status` against its tier's normal window.
3. Flag only what is late **for its own tier**.
4. When a position is inside its window, say so explicitly. "Normal for Apex at day 9" is
   a useful report line; silence reads as a problem.
