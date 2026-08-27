# Fractional-share treatment: does the round-up reach a beneficial owner?

This is the gate the entire reverse-split-arbitrage thesis rests on. Read it before
assigning a verdict to any calendar signal.

## Why "beneficial level" is the whole question

The thesis is that a 1-share position becomes a rounded-up whole share after a reverse
split. That only pays if the round-up reaches the **individual account**.

Many issuers round up only at the **record-holder** level. Shares held in a brokerage
account are held in street name, which means the record holder is Cede & Co. (DTC's
nominee) on behalf of every broker's omnibus position. A record-only round-up therefore
happens **once, at DTC**, across millions of shares, and never reaches any individual
beneficial owner. The headline reads "fractional shares will be rounded up to a whole
share" in both cases. One is worth a full share per account; the other is worth nothing.

So the question is never "does this split round up?" It is **"does this split round up for
beneficial owners holding in street name?"**

## Stage 1 — Structural rejects (no filing fetch needed)

Reject these from the calendar row alone. They cost nothing to eliminate, so always do
this first.

| Reject | How to spot it | Why |
|---|---|---|
| **ETFs, series trusts, funds** | Issuer name contains ETF, Trust, Fund, Portfolio, or the row is a series of a trust (e.g. "Tidal Trust II", "ETF Opportunities Trust") | Fund-level reverse splits pay cash in lieu. There is no beneficial round-up mechanism at all. |
| **ADRs and foreign ordinaries** | Five-letter ticker ending in F or Y; issuer with no SEC filing history | Ratio changes are administered by the depositary bank, cash in lieu is standard, and terms are frequently never filed with the SEC. |
| **Effective date already passed** | `effective_date` earlier than today | The buy window is closed. |
| **NULL effective date** | `effective_date` is null | Not a reject, but never a recommended pick. Verify the real date from a secondary source first. |

Worked example from the 2026-08-27 queue: WZRD, DAMD, ASTN, and STSM are all ETF/trust
series and die here. BBKCF is a foreign ordinary and dies here. VMAR's effective date had
passed and dies here. One signal, KAPA (Kairos Pharma, Ltd.), survives to stage 2.

## Stage 2 — Read the filing

1. **Resolve the ticker to a CIK.** Either `https://www.sec.gov/files/company_tickers.json`
   (a full ticker→CIK map) or a company search:
   `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>&type=8-K&output=atom`.
   The SEC requires a descriptive `User-Agent` header identifying the requester on every
   request; requests without one are throttled or refused.
2. **Fetch the filings that carry the terms**, in order of usefulness:
   - the **8-K** announcing or effecting the split,
   - its **EX-99.1** press release exhibit, which usually states the fractional treatment
     in plainer language than the 8-K body,
   - the **DEF 14A / PRE 14A** proxy when shareholder approval was required, which
     typically contains the most explicit discussion of street-name treatment.
3. **Assign one of four verdicts**, and record the sentence that produced it.

## Verdict vocabulary

| Verdict | Signature language | Action |
|---|---|---|
| `round_up_beneficial` | Round-up language ("each stockholder who would otherwise be entitled to a fractional share will instead receive one whole share") **plus** explicit inclusion of beneficial owners: "beneficial owners", "shares held in street name", "brokers, banks and other nominees will be treated in the same manner as registered holders" | Candidate. Continue to threshold evaluation. |
| `round_up_record_only` | Round-up language **qualified** by "holders of record" or "record holders", and/or deferred for street name: "beneficial owners should contact their broker", "the treatment of fractional shares for beneficial holders will depend on the policies of their broker, bank or other nominee" | Dismiss: `record-holder round-up only; street-name positions cash out` |
| `cash_in_lieu` | "in lieu of any fractional share", "will receive a cash payment in lieu of", "no fractional shares will be issued" | Dismiss: `cash in lieu of fractional shares` |
| `unknown` | No filing located, language absent, or genuinely ambiguous | Never a candidate. Flag in the review table. Dismiss as `fractional treatment unverified` only once the effective date has passed; before then leave the signal at `new` so a later filing can be picked up. |

### Two rules that are not negotiable

- **Never recommend a buy on `unknown`.** Absence of evidence is not evidence of
  round-up. Most reverse splits do not pay at the beneficial level, so the base rate is
  against you and silence should be read pessimistically.
- **Always quote the language.** The review table carries the sentence the verdict rests
  on, so the operator can check the reasoning before approving real money. A verdict you
  cannot source is an `unknown`.

The qualified-round-up trap is the most common way to lose here: the press release says
shares will be rounded up, and a separate paragraph twenty lines down says beneficial
holders should consult their broker. Both statements are in the same document. Read to the
end of the fractional-shares discussion before deciding.

## Stage 3 — The second gate: will the broker honour it?

Issuer terms are necessary but not sufficient. The broker has to actually pass the
round-up through to the account.

Per `references/broker-settlement.md`, Schwab processes cash in lieu only, so a genuine
`round_up_beneficial` split still pays nothing in a Schwab account. Expected value is
therefore **per broker, not per signal**: a play's upside is the count of enabled brokers
expected to honour the round-up, not the count of enabled brokers.

Report that count in the review table alongside the verdict. Do not silently drop brokers
from the fan-out on this basis; state the arithmetic and let the operator decide.
