# Agent Runbook

This file is a fast path for automated agents working in this repository.

## Canonical Commands

- Install dependencies: `uv sync`
- Configure broker credentials: `uv run --python 3.14 python src/main.py setup`
- Show CLI help: `uv run --python 3.14 python src/main.py --help`
- Run full local validation: `./scripts/verify.sh`

## Safe Smoke Tests (No Orders)

Use these checks first. They should not place trades.

1. `uv run --python 3.14 python src/main.py --help`
2. `uv run --python 3.14 python stockshotgun --help`

## Validation Workflow

Always run this before reporting task completion:

1. `./scripts/verify.sh`
2. If verification fails, fix only issues related to your changes
3. Re-run `./scripts/verify.sh`

## Task Recipes

### Add a Broker

1. Create `src/brokers/<broker>.py` using existing broker module patterns
2. Ensure trade/holdings methods are async and call `await rate_limiter.wait_if_needed("BrokerName")` first
3. Wrap blocking SDK calls with `await asyncio.to_thread(...)`
4. Add broker metadata to `BrokerConfig.BROKERS`
5. Register trade/holdings functions in `src/tui/broker_functions.py`
6. Add credentials prompts in `src/setup.py`
7. Run `./scripts/verify.sh`

### Change Order Execution Logic

1. Edit `src/order_processor.py`
2. Keep concurrent execution behavior intact
3. Ensure CLI and TUI pathways still route through `order_processor`
4. Run `./scripts/verify.sh`

### Change CLI/TUI Routing

1. Update `src/main.py` for argument and mode routing
2. Preserve no-arg -> TUI, arg-based -> CLI behavior
3. Run `uv run --python 3.14 python src/main.py --help`
4. Run `./scripts/verify.sh`

## Constraints and Safety

- Never commit `.env` or credential files
- Do not run buy/sell commands as smoke tests
- Prefer minimal changes over large refactors for bugfixes
- Follow existing async-first patterns in broker modules

## High-Signal File Map

- Entry point: `src/main.py`
- Setup wizard: `src/setup.py`
- Concurrent order engine: `src/order_processor.py`
- Broker interface and shared infra: `src/brokers/base.py`
- Broker session lifecycle: `src/brokers/session_manager.py`
- TUI app entry: `src/tui/app.py`
- Broker dispatch table: `src/tui/broker_functions.py`
