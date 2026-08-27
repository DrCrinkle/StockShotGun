# Spec: RSA Agent Plugin (portable agent-plugin packaging)

- **Status**: Approved design (2026-08-27)
- **Relates**: ADR 0003 (MCP fan-out), ADR 0004 (broker registry), ADR 0006 (engine as core), `specs/agent-operated-rsa-engine.md`, `specs/rsa-agent-operator-plan.md`
- **Supersedes**: the "operator skill lives only in `~/.claude/skills/_RSATRADER/`" arrangement from `specs/rsa-agent-operator-plan.md`. That plan's Phase-1 human-gating decisions are unchanged.

## Problem

Two problems, one root cause.

**1. Signal quality.** `calendar_signals` currently stages every reverse split Nasdaq reports, filtered only on ratio direction (`num < den` in `src/signals/nasdaq.py`). Of the 7 rows at `status='new'` on 2026-08-27, four are ETF/trust splits (WZRD, DAMD, ASTN, STSM) that structurally cash out fractional shares, and two (BBKCF, VMAR) had already passed their effective date. Exactly one, KAPA, is a real candidate. The RSA thesis depends on fractional shares being **rounded up at the beneficial-owner level**; nothing in the pipeline checks that.

**2. Brain/hands drift.** The judgment layer (`_RSATRADER` skill) lives in `~/.claude/skills/`, outside this repo. The execution layer (`ssg-router` MCP over `ExecutionEngine`) lives here. They version independently, and the skill's workflow files document this repo's tool contract. That drift is already real: `Workflows/Sweep.md` and `Workflows/Status.md` both invoke `.venv/bin/python main.py status` from the repo root, but the entrypoint moved to `src/main.py`. Both commands fail today, and no test in either tree catches it.

The tempting fix for (1) is to build 8-K fetching and filing classification into the app. That is the wrong layer, and it is what this spec rejects.

## Decision

**The application is hands and ledger. The agent is the brain. The plugin is how they ship together.**

This restates a principle `ISA.md` already commits to: *"Code enforces safety, prompts do not"* and *"What the agent decides (when, what, how much, which brokers) is named in prose; what the executor enforces is named in code."*

Consequences:

- Judging whether a split rounds up at the beneficial level is **prose in a skill reference doc**, not code. The agent reads the 8-K itself with tools it already has. Zero filter code enters `src/`.
- `calendar_signals` stays a mechanical fetch/dedupe ledger. It never learns what a good play is. `src/signals/nasdaq.py` is not modified.
- The skill, its references, and the MCP server configuration are packaged as one versioned unit inside this repo, conforming to the [Agent Plugins](https://agent-plugins.org) 1.0.0 spec so it is not tied to a single agent client.
- Personal configuration (thresholds, notification wiring, execution logging) stays outside the repo, because this repo is public.

### Non-goals

- No EDGAR client, filing parser, issuer-type classifier, or round-up heuristic in `src/`.
- No change to `src/signals/nasdaq.py`, `src/brokers/`, or the enforcement gates.
- No Phase-2 auto-execute. Every buy and sell stays human-gated per `specs/rsa-agent-operator-plan.md`.
- No new persistence. The existing SQLite tables are unchanged.

## Package layout

```
StockShotGun/
├── plugin/
│   ├── plugin.json                       # Agent Plugins 1.0.0 manifest (portable)
│   ├── mcp.json                          # ssg-router, stdio (portable)
│   ├── skills/
│   │   └── rsa-operator/
│   │       ├── SKILL.md
│   │       └── references/
│   │           ├── review.md
│   │           ├── sweep.md
│   │           ├── status.md
│   │           ├── fractional-treatment.md
│   │           └── broker-settlement.md
│   ├── .claude-plugin/plugin.json        # Claude Code manifest
│   └── .mcp.json                         # Claude Code MCP config
└── .claude-plugin/marketplace.json       # installable directly from the repo
```

Three ownership layers:

| Layer | Location | Public? | Contents |
|---|---|---|---|
| Portable core | `plugin/` | yes | manifests, `mcp.json`, `rsa-operator` skill + references |
| Client namespace | `plugin/.claude-plugin/`, `plugin/.mcp.json`, repo-root `marketplace.json` | yes | Claude Code discovery files |
| Personal shim | `~/.claude/skills/_RSATRADER/` | no | voice notify, execution log, threshold path, Pulse cron scripts |

### Dual manifests

The Agent Plugins spec puts `plugin.json` and `mcp.json` at the package root. Claude Code discovers `.claude-plugin/plugin.json` and `.mcp.json`. Both are shipped; the pair is small and fully static. Precedent: the installed `superpowers` plugin carries `.claude-plugin/`, `.codex-plugin/`, `.cursor-plugin/`, `.kimi-plugin/`, and `gemini-extension.json` side by side in one tree.

### `plugin.json`

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
  "name": "stockshotgun-rsa",
  "version": "0.1.0",
  "description": "Reverse-split-arbitrage operator: multi-broker order fan-out, RSA position ledger, and human-gated buy/sell workflows over the ssg-router MCP.",
  "license": "MIT",
  "repository": "https://github.com/DrCrinkle/StockShotGun",
  "keywords": ["trading", "reverse-split", "arbitrage", "mcp", "brokers"]
}
```

Required by spec: `$schema` and `name` only. `name` must be 1-64 chars, lowercase alphanumeric with hyphens and periods.

### `mcp.json`

```json
{
  "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
  "mcpServers": {
    "ssg-router": {
      "type": "stdio",
      "command": ".venv/bin/python",
      "args": ["-m", "agentic.router"],
      "cwd": "${PLUGIN_ROOT}/..",
      "env": {
        "PYTHONPATH": "${PLUGIN_ROOT}/../src",
        "SSG_DB_PATH": "${SSG_DB_PATH}"
      }
    }
  }
}
```

This replaces the current global registration in `~/.claude.json`:

```json
{"command": "bash", "args": ["-lc", "PYTHONPATH=src exec .venv/bin/python -m agentic.router"]}
```

The `bash -lc "cd ... && exec"` wrapper existed only because there was nowhere to declare a working directory. `cwd` removes the need for a shell in the chain.

## The skill

One skill, `rsa-operator`. Review, Sweep, and Status are three modes of one job; separate skills would multiply frontmatter for no gain.

### Frontmatter

Conforms to the Agent Skills specification: `name` must match the directory name and use only lowercase alphanumerics and single hyphens; `description` is capped at 1024 characters and must say both what the skill does and when to use it.

```yaml
---
name: rsa-operator
description: >
  Drives the reverse-split-arbitrage lifecycle over the ssg-router MCP with human-gated
  buys and sells: scan the Nasdaq split calendar, verify fractional-share treatment rounds
  up at the beneficial-owner level, evaluate against configured thresholds, stage and
  execute approved buys across enabled brokers, then sweep and sell post-split positions.
  Every buy and sell requires the operator's explicit per-item approval in session.
  Use when the user mentions RSA, reverse splits, reviewing signals, sweeping, selling
  arrived shares, or checking the splits calendar.
license: MIT
compatibility: Requires Python 3.14+, uv, and configured broker credentials in .env
metadata:
  repository: "https://github.com/DrCrinkle/StockShotGun"
---
```

### Body

Ported from the existing `_RSATRADER/SKILL.md`, minus everything personal.

| Existing section | Destination |
|---|---|
| Workflow routing table | portable `SKILL.md` (paths repointed to `references/`) |
| Gotchas | portable `SKILL.md`, verbatim |
| Examples | portable `SKILL.md`, names genericized |
| `Workflows/Review.md`, `Sweep.md`, `Status.md` | `references/review.md`, `sweep.md`, `status.md` |
| Voice Notification | personal shim only |
| Execution Log | personal shim only |
| Customization (LifeOS path) | `RSA_PREFERENCES` env var |
| "Taylor" | "the operator" |

The Gotchas section carries the highest value and ports unchanged: dismiss-never-promote, `record_rsa_trade`'s three distinct refusal modes (check `trade_id`, not just `ok`), NULL `effective_date` as dangerous rather than benign, `dry_run=true` as a full-pipeline rehearsal, the account-scoped dispatch guard on the 12 account-blind brokers, approved-is-not-executed, and never operating on the live DB with test data.

### Review gains a verdict gate

`references/review.md` keeps its existing step order (notify, load thresholds, scan, evaluate, present one table, per-item approval, stage, execute, record, dismiss) with one insertion: a **fractional-treatment verdict** step runs immediately after `scan_signals` and before threshold evaluation, applying `references/fractional-treatment.md`. Signals rejected at Stage 1 never cost a filing fetch. The presented table gains two columns: `verdict` and the filing language the verdict rests on, so the operator can check the reasoning before approving. A signal whose verdict is anything other than `round_up_beneficial` can never be the recommended pick, regardless of ratio or cost.

### Thresholds

The portable skill reads thresholds from the file named by `RSA_PREFERENCES`, defaulting to `${PLUGIN_DATA}/preferences.md`. Keys: `min_ratio`, `per_play_cap_usd`, `max_share_price_usd`, `min_days_to_effective`, `enabled_brokers`.

**The fail-closed rule ports verbatim: if the preferences file is absent, the skill refuses to stage a live buy.** Real money has no safe default threshold. This is also what makes public packaging acceptable: the repo ships the procedure, never the numbers.

## `references/fractional-treatment.md`

The heart of this spec. Prose the agent reads before recommending any signal.

### Why beneficial level is the whole question

The RSA thesis is that a 1-share position becomes a rounded-up whole share after a reverse split. That only pays if the round-up reaches the individual account. Many issuers round up only at the **record-holder** level. Street-name shares are held by Cede & Co. as a single record holder, so a record-only round-up happens once at DTC and never reaches any beneficial owner. Terms that look identical in a headline can be worth a full share or worth nothing.

### Stage 1: structural rejects, no filing needed

- **ETFs, series trusts, and funds.** Issuer name contains ETF, Trust, Fund, or Portfolio, or the row is a series of a trust. Fund-level reverse splits pay cash in lieu; there is no beneficial round-up mechanism. Rejects WZRD, DAMD, ASTN, STSM from the current queue at zero cost.
- **ADRs and foreign ordinaries.** Five-letter tickers ending in F or Y, and issuers with no SEC filing history. Ratio changes are administered by the depositary bank, cash in lieu is standard, and terms are frequently not filed with the SEC at all. Covers BBKCF.
- **Effective date already passed.** Reject and dismiss; the buy window is closed.

### Stage 2: read the filing

1. Resolve ticker to CIK via `https://www.sec.gov/files/company_tickers.json`, or search `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&company=<name>&type=8-K&output=atom`. SEC requires a descriptive `User-Agent` header on every request.
2. Fetch the most recent 8-K covering the split, its `EX-99.1` press release exhibit, and the proxy (`DEF 14A`/`PRE 14A`) when shareholder approval was required.
3. Classify into one of four verdicts.

### Verdict vocabulary

| Verdict | Signature language | Action |
|---|---|---|
| `round_up_beneficial` | Round-up language ("entitled to a fractional share will instead receive one whole share") **plus** explicit inclusion of beneficial owners: "beneficial owners", "shares held in street name", "brokers, banks and other nominees will be treated in the same manner" | Candidate. Continue to threshold checks. |
| `round_up_record_only` | Round-up language **qualified** by "holders of record" or "record holders", and/or a deferral such as "beneficial owners should contact their broker" or "treatment of fractional shares for beneficial holders will depend on the policies of their broker or nominee" | Dismiss: `record-holder round-up only; street-name positions cash out` |
| `cash_in_lieu` | "in lieu of any fractional share", "will receive a cash payment", "no fractional shares will be issued" | Dismiss: `cash in lieu of fractional shares` |
| `unknown` | No filing located, language absent or genuinely ambiguous | Never a candidate. Flag in the review table. Dismiss as `fractional treatment unverified` only once the effective date has passed; before then leave at `new` so a later filing can be picked up. |

**Never recommend a buy on `unknown`.** Absence of evidence is not evidence of round-up.

### Stage 3: the second gate

Issuer terms are necessary but not sufficient. The broker must actually honor the round-up. Per `references/broker-settlement.md`, Schwab processes cash-in-lieu only, so a `round_up_beneficial` split still pays nothing in a Schwab account. Expected value is per-broker, not per-signal. The review table reports the candidate's verdict alongside how many enabled brokers are expected to honor it.

## `references/broker-settlement.md`

Clearing-tier rationale, not numbers. Numbers live in `BROKER_PROFILES` in `src/sweep.py`, and the reference doc points there rather than duplicating values that drift.

- **Apex-cleared** (BBAE, DSPAC, Firstrade, Public, SoFi, Webull): 3+ week processing since the Nov 2024 policy change. Holdings legitimately read 0 shares for weeks. Webull is Apex via an omnibus model, not self-clearing.
- **Fractional-first** (Robinhood, TastyTrade): a fractional share is delivered before any round-up. TastyTrade fractionals may be permanently unsellable.
- **Self-clearing** (Schwab, Wells Fargo, Chase): Schwab is cash-in-lieu only. Wells Fargo follows issuer terms but cannot buy OTC under $1. Chase restricts OTC under $5.
- **Trading-blocked** (Fennel): blocks trading until the share arrives.
- **Unknown** (Tradier): RQD Clearing, expensive reorganization fees.

The sweep workflow judges each position against its own broker's tier, never against the fastest broker.

## The personal shim

`~/.claude/skills/_RSATRADER/SKILL.md` reduces to frontmatter plus three blocks:

1. The voice-notify curl to `localhost:31337/notify`.
2. The execution-log JSONL append to `MEMORY/SKILLS/execution.jsonl`.
3. `RSA_PREFERENCES` pointed at `~/.claude/LIFEOS/USER/SKILLCUSTOMIZATIONS/_RSATRADER/PREFERENCES.md`, and `SSG_DB_PATH` pointed at the live store.

Body reduces to: defer to the plugin's `rsa-operator` skill for all methodology. `Workflows/`, Gotchas, and Examples are deleted here. `Tools/DailyDigest.sh` and `Tools/DigestHealth.sh` stay, being Pulse cron jobs with no meaning outside LifeOS. The `_RSATRADER` trigger vocabulary is retained so existing phrasing keeps working.

Because the shim contains no methodology, there is nothing in it that can drift from the code.

## The one code change

`src/execution/engine.py:40` sets `DEFAULT_RSA_STORE_PATH = "logs/automation.sqlite3"`, a **relative** path. The process working directory therefore decides which database a real-money write lands in. Today that is held together by the `cd` inside the bash wrapper in `~/.claude.json`.

Packaging must not re-encode that fragility in `mcp.json`. The change:

1. `ExecutionEngine` honors `SSG_DB_PATH` when set, resolving it absolutely; the existing relative default is retained when it is unset, so CLI and TUI behavior is unchanged.
2. The `rsa-operator` skill refuses to stage a live buy when the resolved store path is not absolute.

Same fail-closed shape as the thresholds rule. It makes "wrote to the wrong database" an impossible state rather than a working-directory accident.

## Migration

1. Create `plugin/` with both manifest pairs and `mcp.json`.
2. Port `SKILL.md` and the three workflows into `plugin/skills/rsa-operator/`, stripping personal content and repointing `Workflows/` to `references/`.
3. Fix the stale entrypoint in the ported sweep and status references: `main.py status` becomes `src/main.py status`.
4. Write `references/fractional-treatment.md` and `references/broker-settlement.md`.
5. Land the `SSG_DB_PATH` change plus its tests.
6. Reduce `~/.claude/skills/_RSATRADER/` to the shim.
7. Remove the `ssg-router` entry from `~/.claude.json` and install the plugin from the repo.
8. Verify in a fresh session: `rsa review` reaches a recommended pick, or explains why none qualifies.

## Testing

- Schema validation of `plugin.json` and `mcp.json` against the published 1.0.0 schemas, in `scripts/verify.sh`.
- `skills-ref validate plugin/skills/rsa-operator` for Agent Skills conformance.
- A test asserting every shell command appearing in the skill's reference docs names a path that exists in the repo. This is the test that would have caught the `main.py` drift, and it is the mechanism that keeps brain and hands honest.
- Unit tests for `SSG_DB_PATH`: set/absolute, unset/relative-default, and relative-value rejection.
- No test may make a live broker call or touch `logs/automation.sqlite3`.

## Risks

- **Public repo, real money.** Mitigated by keeping thresholds, account shape, and DB path outside the repo, and by both fail-closed rules.
- **Spec immaturity.** Agent Plugins 1.0.0 is young. The portable manifests are static files; if the spec churns, the cost is editing two small JSON files.
- **Verdict misclassification.** An agent could read `round_up_record_only` as `round_up_beneficial` and lose the play's cost. Bounded by `per_play_cap_usd`, and the review table must quote the filing language it based the verdict on so the operator can check it before approving.
