
<h1 align="center">StockShotGun</h1>
<p align="center">
  A one click solution to submitting orders to multiple brokers at the same time
</p>

## About The Project
I partake in [Reverse Split Arbitrage](https://www.reversesplitarbitrage.com/) and wanted to semi-automate the buying and selling of tickers that were going through a reverse split instead of scrambling around each brokerage to get orders in manually.

## Current Broker Support
Thirteen brokers, all enabled. `src/brokers/registry.py` is the source of truth.

* **Robinhood**: username, password and MFA setup token
* **Tradier**: access token
* **TastyTrade**: OAuth client id, client secret and refresh token
* **Public**: API secret
* **Firstrade**: username, password and MFA token
* **Fennel**: personal access token
* **Schwab**: API key and secret (OAuth; token cached in `tokens/`)
* **BBAE Pro**: email and password
* **dSPAC**: email and password
* **SoFi**: username, password, optional TOTP secret
* **Webull**: pre-obtained browser credentials (`WEBULL_PROFILES` JSON)
* **Wells Fargo**: username and password (browser automation)
* **Chase**: username and password (browser automation)

## Getting Started
First you will need to set up authentication
```
git clone <this-repo> && cd StockShotGun
uv sync
./stockshotgun setup
```
The set up will ask for your API keys or credentials and add them to a ```.env``` file

## Usage
To buy a ticker at market
```
./stockshotgun buy 1 TSLA
```
To sell a ticker at market
```
./stockshotgun sell 1 TSLA
```
To make a limit order, add a price after the ticker
```
./stockshotgun buy 1 TSLA 650.45
```
Run with no arguments for the interactive TUI. The `./stockshotgun` shim puts
`src/` on `sys.path`; `python3 src/main.py <args>` is equivalent.

## Agentic mode

StockShotGun also exposes itself as an MCP server, so any MCP-aware agent
(Claude, a local DA, an external orchestrator) can drive the full multi-broker
fan-out through one tool surface. Compared to a single-broker MCP like
Robinhood's first-party offering, StockShotGun gives the agent one call that
fans out across all 13 integrated brokers — the coordination problem this
project was built to solve.

**Architecture** — one MCP server process per broker (thirteen possible, all from
a single generic entrypoint) + one router MCP fronting them + one shared
`enforcement` library every order path imports. The router
is the agent's surface; per-broker MCPs isolate credentials + crash blast
radius; enforcement runs the safety gates (dollar limits, freeze list, per-leg
confirmation tokens with intent-binding hashes, idempotency, audit log).
See [ADR 0003](docs/adr/0003-mcp-fanout-architecture.md) for the decision
rationale and [docs/agentic/RSA_AGENT.md](docs/agentic/RSA_AGENT.md) for the
RSA agent workflow.

### 30-second demo

```bash
# 1. Clone + install
git clone <this-repo> && cd StockShotGun && uv sync

# 2. Configure credentials (interactive)
./stockshotgun setup

# 3. List brokers + their health
PYTHONPATH=src uv run python -m agentic.cli --json list-brokers

# 4. Dry-run a fan-out preview (no SDK calls, no live orders)
PYTHONPATH=src uv run python -m agentic.cli --json dry-run buy 1 TSLA \
  --brokers Fennel --price 5.0

# 5. Two-step live order (propose → review → execute)
PYTHONPATH=src uv run python -m agentic.cli --json propose buy 1 TSLA \
  --brokers Fennel --price 5.0 --live
# → returns {"proposal_id": "abc123…", "estimated_usd": 5.0, ...}
PYTHONPATH=src uv run python -m agentic.cli --json execute abc123… --live

# 6. Walk the tamper-evident audit log
PYTHONPATH=src uv run python -m agentic.cli --json audit-verify
```

Every order routes through the enforcement gate before any broker SDK runs.
Live orders require the explicit two-step propose → execute flow — there is
no single-call live order primitive, by design.

### Starting the router MCP for agent consumption

```bash
# Stdio MCP server — connect from Claude Desktop, MCP Inspector, etc.
PYTHONPATH=src uv run python -m agentic.router

# Per-broker MCP servers (one process per broker, for full credential
# isolation when running over the network). One generic entrypoint serves any
# broker in the registry — ADR 0004 superseded ADR 0003's per-broker modules.
PYTHONPATH=src uv run python -m agentic.broker Fennel
```

The router exposes 13 agent-facing tools:

| Tool | Purpose |
|------|---------|
| `list_brokers` | Per-broker health + capability flags |
| `get_holdings` | Fan-out holdings query (credential-shaped fields stripped) |
| `propose_order` | Mint a fan-out proposal — runs full safety pipeline |
| `execute_order` | Execute a previously minted proposal with per-leg tokens |
| `place_order` | Convenience: propose + dry-execute in one call |
| `get_rsa_trade` | Read an RSA trade's positions + sweep state |
| `run_sweep` | Classify positions against current broker holdings |
| `sell_arrived` | Propose sells for every ARRIVED leg in an RSA trade |
| `record_rsa_trade` | Register an agent-placed buy into the sweep lifecycle |
| `recap_ingest` | Parse a chat recap into buy / research / TBA pipelines |
| `scan_signals` | Poll the Nasdaq splits calendar for reverse-split signals |
| `promote_signal` | Promote a calendar signal into the buy queue |
| `dismiss_signal` | Dismiss a calendar signal with a reason |

## Special Thanks
* [NelsonDane](https://github.com/NelsonDane/)
  * [public-invest-api](https://github.com/NelsonDane/public-invest-api)
  * [fennel-invest-api](https://github.com/NelsonDane/fennel-invest-api)

## To Do
* Add encryption to credentials
* Fully automate by tracking FINRA and/or SEC filings
* Add more brokers
* Add per trade logging to a CSV
* maybe add a menu with entries for buy sell setup, to avoid having to rerun script after setup.
