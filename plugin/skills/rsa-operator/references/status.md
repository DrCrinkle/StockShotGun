# Status

**Goal:** A quick, read-only digest of RSA state. No MCP calls, no approvals, no state
changes.

## Steps

1. Run `src/main.py status --output json` with the repo's virtualenv interpreter, from the
   repo root.
2. Render a compact digest from the JSON:
   - **Open trades** — one line each: ticker, split ratio, expected split date, position
     count and their statuses.
   - **Positions by state** — roll up all positions across trades by `status` (e.g.
     `never-swept`, `share_arrived`, `sold`), so the shape is visible at a glance rather
     than only per-trade.
   - **Signal counts** — `calendar_signals`, `buy_signals`, `pending_sell_triggers`, each
     broken down by status bucket.
3. If the command fails (non-zero exit, unparseable JSON), report the raw error. Do not
   guess at state.

No approval gates apply. This workflow never calls `propose_order`, `execute_order`,
`run_sweep`, or any other state-changing tool.
