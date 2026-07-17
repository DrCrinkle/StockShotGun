# RSA Agent Operator Plan (Plan 2 of 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The PAI-side operator for the agent-operated RSA engine (`specs/agent-operated-rsa-engine.md`): a private `_RSATRADER` skill, a $0 scheduled daily digest job, Pulse observability, and the MCP wiring that lets a Claude session drive `ssg-router` — with every buy human-approved (Phase 1).

**Architecture:** Detection and notification are deterministic and free: a Pulse `[[job]]` (`type="script"`) runs `signals scan` + `status` daily and pushes a Telegram digest (or a `NO_ACTION` sentinel). Judgment is conversational: Taylor opens a session, the `_RSATRADER` skill drives evaluation → per-item approval (JobSearch pattern: one recommended pick, "yes" per item) → `propose_order` → show estimate → approve → `execute_order`, all against the globally-registered `ssg-router` stdio MCP. One repo-side precursor closes the trade-capture gap so agent buys are sweepable. Auto-execute (Phase 2) is explicitly out of scope.

**Tech Stack:** StockShotGun (.venv python 3.14, pytest — 306 passing baseline post-PR#3), LifeOS tree at `~/.agents/LIFEOS/` (Pulse daemon: bun, `PULSE/pulse.ts` + `PULSE.toml`), Telegram dispatch (enabled), `~/.claude.json` for global MCP registration.

**Decisions already made (do not relitigate):**
- Phase 1 = human-gated buys, per-item conversational approval. Sells of arrived shares also flow through approval in Phase 1 (bounded downside, but consistency first).
- The skill does **NOT** use the `automate` due-buy path. Buys happen directly via `propose_order`/`execute_order` in-session. This resolves the deferred I2 finding (calendar buys riding along recap runs): calendar signals handled by the agent are **dismissed with reason `bought via _RSATRADER <date> trade #<id>`** after the buy executes — never promoted — so the `automate` queue can never double-fire them. `promote_signal` remains for the recap-style flow only.
- Daily job stays `type="script"` ($0). No `type="claude"` job in Phase 1 (those are deliberately tool-less; see PULSE `lib.ts:272-316` — and never replicate `claude` shelling without stripping `ANTHROPIC_API_KEY`; the April 2026 invoice is the reason).
- Skill name: `_RSATRADER` (private `_ALLCAPS`). Real brokerage context fails CreateSkill's public bright-line test. The spec's earlier "RsaTrader" name is superseded.

**Recon facts** (verify against live files; sources: `~/.agents/LIFEOS/DOCUMENTATION/Skills/SkillSystem.md`, `PULSE/pulse.ts`, `PULSE/lib.ts`, `PULSE/PULSE.toml`, `~/.claude.json`):
- Skill format: frontmatter `name` + single-line `description` (≤650 chars HARD — over silently drops the skill) containing `USE WHEN`; body order: title → `## Voice Notification` (curl to `localhost:31337/notify`) → `## Workflow Routing` table → `## Gotchas` → `## Examples` → `## Customization`; only `Workflows/`, `Tools/`, `References/` subdirs; log one JSONL line to `~/.claude/PAI/MEMORY/SKILLS/execution.jsonl` per workflow run.
- Pulse jobs: `[[job]]` blocks with `name/schedule/type/command|prompt/output/enabled`; script jobs run `bash -c <command>` with cwd = PULSE dir; sentinel outputs (`NO_ACTION` etc.) suppress dispatch; **3 consecutive failures → job silently skipped** (`MAX_FAILURES`).
- Dispatch: `telegram` (enabled, ≤4096 chars, Markdown) is the live push channel; `voice` currently disabled; `ntfy`/`email`/`log` available.
- Pulse modules are code-registered: `modules/<name>.ts` exporting `start/stop/health/handleRequest` + FIVE hand-edits in `pulse.ts` (module var, `loadModules` import gate, `main()` start, fetch route branch, `buildHealthResponse` entry) + a top-level `[<name>]` table in PULSE.toml. No auto-discovery.
- MCP: global registration in `~/.claude.json` `mcpServers` (stdio shape like zendriver). StockShotGun has NO project `.mcp.json`. src-layout means `python -m agentic.router` needs the venv python + `cwd`/`PYTHONPATH=src` unless editable-installed.

---

## Task 1 (repo-side): close the agent trade-capture gap

**Repo:** /home/taylor/projects/StockShotGun, new branch `feat/rsa-operator-support` off main (after PR #3 merges; if not merged yet, branch off `feat/adr-0006-completion` and say so).

**Problem:** sweep/sell (`run_sweep`, `sell_arrived`) key off `rsa_trades`/`rsa_positions`, which are persisted by the **CLI buy command only** (see memory/spec: "RSA capture at buy time"). An agent buying via MCP `propose_order`/`execute_order` creates no trade record → its buys are invisible to the sweep lifecycle.

- [ ] Read how `cli/trade.py`/`cli/batch.py` capture buys into `RsaStore` (find the exact call sites and what they persist per executed leg) — the new tool must produce identical rows.
- [ ] TDD: engine method + FastMCP tool `record_rsa_trade(ticker, split_ratio, expected_split_date, execution, signal_id=None) -> {ok, trade_id, position_count}` — takes the execute_order result dict, persists one `rsa_trades` row + one `rsa_positions` row per `ok=True` leg (broker, account_id, qty). Reject (`ok:false`) when the execution has zero ok legs or `dry_run=True` (rehearsals must not create trades). `signal_id` links back to `calendar_signals.id` when the play came from a scan.
- [ ] Tool docstring (agent-facing): call this immediately after a successful live `execute_order` for an RSA buy; without it the play cannot be swept or sold.
- [ ] Extend the router tool-registration whitelist test; full suite green (306 + new). Commit, push, PR (small, standalone).

## Task 2: register `ssg-router` globally

- [ ] Verify editable-install status: `/home/taylor/projects/StockShotGun/.venv/bin/python -c "import agentic.router"` from an arbitrary cwd. If it imports, omit env/cwd; else include both.
- [ ] Add to `~/.claude.json` `mcpServers` (top-level, NOT project-scoped — sessions start anywhere):
  ```json
  "ssg-router": {
    "command": "/home/taylor/projects/StockShotGun/.venv/bin/python",
    "args": ["-m", "agentic.router"],
    "cwd": "/home/taylor/projects/StockShotGun",
    "env": {"PYTHONPATH": "/home/taylor/projects/StockShotGun/src"}
  }
  ```
  Edit surgically (jq or careful Edit — the file holds all other server registrations; back it up first to the session scratchpad).
- [ ] Verify from a FRESH Claude session: `scan_signals`/`propose_order` etc. appear via ToolSearch and `scan_signals(refresh=false)` round-trips against the real store. Note startup cost: the server builds 13 broker specs lazily — confirm the stdio server starts in <5s without credentials configured (it must not hang on missing .env; test with a temp HOME if unsure).

## Task 3: the `_RSATRADER` skill

**Location:** `~/.claude/skills/_RSATRADER/` (private — never in a public release). Config in `~/.claude/PAI/USER/SKILLCUSTOMIZATIONS/_RSATRADER/PREFERENCES.md`.

- [ ] `SKILL.md` per canonical format. Description (single line, ≤650 chars) must carry: USE WHEN rsa, reverse split, rsa review, review signals, rsa sweep, sell arrived, rsa status, run the rsa play, check the calendar / NOT FOR general trading chat or Robinhood-only queries (robinhood-trading MCP). Body: Voice Notification block, Workflow Routing table, Gotchas, Examples, Customization block, execution-log convention.
- [ ] `Workflows/Review.md` — the buy flow. Steps: notify → `scan_signals(refresh=true)` → load thresholds from PREFERENCES.md → for each `new` signal, evaluate (ratio ≥ min_ratio; est. cost = price × enabled-broker-account-count ≤ per_play_cap; effective date ≥ min_days_out; **flag NULL effective_date loudly** — immediately-due semantics) → present a table with ONE recommended pick (JobSearch pattern) → per-item "yes" → `propose_order(1 share, all enabled brokers, dry_run=false)` → show estimate + leg count → explicit second "yes" to execute → `execute_order` → `record_rsa_trade(..., signal_id=...)` → `dismiss_signal(reason="bought via _RSATRADER <date> trade #<id>")` → summary + execution-log line. Rejected/skipped signals: `dismiss_signal` with the concrete reason. GateError or `rejected` execution → report, never retry silently.
- [ ] `Workflows/Sweep.md` — the post-split flow: `status` snapshot (or `get_rsa_trade`) → for trades past expected split date, `run_sweep(trade_id, dry_run=false)` → summarize per-broker states against the clearing-window expectations → for `share_arrived` legs, `sell_arrived(trade_id)` → present proposal → "yes" → `execute_order`. Note Gotcha: legs whose positions carry real (non-"primary") account ids will be refused by the account-scoped dispatch guard for blind brokers — surface, don't retry.
- [ ] `Workflows/Status.md` — read-only: run `status --output json` via Bash (not MCP) and render the human digest.
- [ ] Gotchas section MUST include: dismiss-not-promote (double-fire prevention, the I2 resolution); record_rsa_trade mandatory after live buys; NULL-date danger; dry_run rehearsal semantics; the account-scoped guard; "approved ≠ executed — every execute needs its own yes in this session".
- [ ] `SKILLCUSTOMIZATIONS/_RSATRADER/PREFERENCES.md` + `EXTEND.yaml`: min_ratio (default 1:5), per_play_cap_usd, max_share_price_usd, min_days_to_effective, enabled_brokers (default: all), notify targets.
- [ ] Test per CreateSkill practice: one with-skill session transcript exercising Review against `dry_run=true` + fixture-ish store; verify the skill actually activates on "rsa review" (description trigger test).

## Task 4: the daily digest job (Pulse)

- [ ] `~/.claude/skills/_RSATRADER/Tools/DailyDigest.sh` (or `.ts` under bun — match what the tool needs; bash is fine): runs `/home/taylor/projects/StockShotGun/.venv/bin/python src/main.py signals scan --output json --db-path logs/automation.sqlite3` (cwd StockShotGun, absolute paths — the job's cwd is the PULSE dir) then `... status --output json`; composes a ≤4096-char Markdown digest: new signals (ticker/ratio/date, NULL-date flagged ⚠), open trades past split date needing sweep, pending buy_signals count. If nothing actionable → print exactly `NO_ACTION`. Scan failure (Nasdaq down) → print a one-line failure notice (do NOT sentinel — Taylor should see repeated failures).
- [ ] `PULSE.toml` `[[job]]` (copy an existing disabled `monitor-*` block's shape):
  ```toml
  [[job]]
  name = "rsa-daily-digest"
  schedule = "30 6 * * 1-5"      # 06:30 PT weekdays ≈ 09:30 ET market open
  type = "script"
  command = "bash /home/taylor/.claude/skills/_RSATRADER/Tools/DailyDigest.sh"
  output = ["telegram", "log"]
  enabled = true
  ```
- [ ] **MAX_FAILURES mitigation** (a silently-skipped trading monitor is unacceptable): add the digest job's health to the existing healthcheck pattern — simplest: the digest script writes a `last_success` timestamp file; a second weekly `[[job]]` (`rsa-digest-health`, Mondays) checks the timestamp age and emits `NO_ACTION` or a "digest job hasn't succeeded since X" alert. Keep both scripts <80 lines.
- [ ] Verify: run the script by hand (both the digest and NO_ACTION paths), then trigger via Pulse once (temporarily set schedule to the next minute or use Pulse's manual-run mechanism if one exists — check `pulse.ts` routes) and confirm the Telegram message arrives.

## Task 5: Pulse `rsatrader` module (observability API)

Minimal scope — API + health only; a dashboard tab is deferred (the Observability Next.js build is its own project).

- [ ] `~/.agents/LIFEOS/PULSE/modules/rsatrader.ts` modeled on `modules/syslog.ts` + `example-module.ts` contract: `start(config)` begins a poll loop (interval from config, default 300s) running the same `status --output json` command via `Bun.spawn`, caching the parsed snapshot; `handleRequest("/api/rsatrader/status")` returns the cached snapshot + `fetched_at`; `health()` returns degraded if the last poll failed or is stale >2 intervals; `stop()` clears the timer.
- [ ] The five `pulse.ts` edit sites (module var, loadModules gate on `config.rsatrader?.enabled`, main() start, fetch branch `pathname.startsWith("/api/rsatrader")`, health subsystem entry) + `[rsatrader]` table in PULSE.toml (`enabled = true`, `poll_seconds = 300`, `stockshotgun_dir = "/home/taylor/projects/StockShotGun"`).
- [ ] Verify: restart the pulse service (`systemctl --user restart com.lifeos.pulse`), `curl localhost:31337/api/rsatrader/status` returns the snapshot, `/healthz` shows the subsystem, and the pulse stderr log is clean. **The LIFEOS tree is a synced git repo — commit these edits there with a clear message; do not leave them uncommitted in a bisync'd tree.**

## Task 6: end-to-end rehearsal + docs

- [ ] Full Phase-1 rehearsal, no live orders: seed a future-dated calendar signal into a COPY of the store (never `logs/automation.sqlite3` for synthetic data — use `--db-path` throughout), run the Review workflow in a real session with `dry_run=true` end to end (scan → evaluate → approve → propose → execute rehearsal → confirm `record_rsa_trade` correctly REFUSES dry-run), run the digest script against the copy, hit the Pulse endpoint.
- [ ] Update `specs/agent-operated-rsa-engine.md` build-order: items 3 (skill), 4 (routine/approval), 6 partially (Pulse feed) → shipped Phase 1; auto-execute flag remains open with its criteria ("clean track record" = N approved plays executed with zero enforcement rejections and zero manual corrections — propose N=5 to Taylor at flip time).
- [ ] Update the project memory note (`rsa-signals-deferred-findings`): I2 resolved by dismiss-not-promote; record the record_rsa_trade tool.
- [ ] Log completion to `execution.jsonl`; commit repo-side doc changes.

---

## Out of scope (explicit)

Phase 2 auto-execute; a `type="claude"` scheduled agent with tools (needs its own guarded design); the Observability dashboard tab; migrating other brokers to account-scoped dispatch; TradeFn threading beyond Fennel; the automate `executing`-state crash-window fix (still an ADR open question).

## Self-review notes

- Ordering: Task 1 (repo tool) → 2 (MCP wiring) → 3 (skill) is a hard dependency chain; 4 and 5 are independent after 2; 6 last.
- Two codebases: Tasks 1 is StockShotGun (branch + PR); 3–5 are the LIFEOS tree (direct commits, it syncs); 2 edits `~/.claude.json` (config, no VCS — back up first).
- Everything real-money-shaped is double-gated: per-item yes to stage, second yes to execute, dry-run rehearsal refuses trade capture, digest failures are loud, silently-skipped jobs get a watchdog.
