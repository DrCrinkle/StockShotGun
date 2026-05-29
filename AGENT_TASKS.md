# Agent Task Recipes

Use these recipes as copy/paste workflows for common requests.

## 1) Bugfix (Minimal Scope)

Goal: fix one behavior without unrelated refactors.

Checklist:
- Reproduce the issue (command + input + expected vs actual)
- Locate the smallest owning module
- Apply minimal fix only in required files
- Confirm no new type/syntax issues
- Run `./scripts/verify.sh`
- Report root cause + exact files changed

Copy/Paste Template:

```text
Task: Fix bug in <module/feature>

Repro:
- Command/input: <...>
- Expected: <...>
- Actual: <...>

Constraints:
- Minimal change only
- No refactor unless required by fix

Done when:
- Repro no longer fails
- ./scripts/verify.sh passes
```

## 2) Add Broker Integration

Goal: add one broker adapter following existing async/rate-limit patterns.

Checklist:
- Add `src/brokers/<broker>.py`
- Implement required functions:
  - `<broker>Trade(side, qty, ticker, price)`
  - `<broker>GetHoldings(ticker=None)`
  - `get_<broker>_session(session_manager)`
- Ensure trade/holdings start with `await rate_limiter.wait_if_needed("BrokerName")`
- Wrap blocking SDK calls with `await asyncio.to_thread(...)`
- Register broker in `BrokerConfig.BROKERS`
- Register functions in `src/tui/broker_functions.py`
- Add credential prompts in `src/setup.py`
- Run `./scripts/verify.sh`

Copy/Paste Template:

```text
Task: Add broker <BrokerName>

Must do:
- Async-first broker functions
- Rate limiter call first in trade/holdings
- Blocking SDK calls wrapped with asyncio.to_thread
- Wire into BrokerConfig + broker_functions + setup prompts

Done when:
- Broker appears in dispatch/config
- ./scripts/verify.sh passes
```

## 3) Add or Change CLI Command

Goal: update CLI behavior while preserving TUI routing.

Checklist:
- Update argument parsing in `src/main.py`
- Keep mode split intact (no args -> TUI, args -> CLI)
- Ensure selected brokers flow remains valid
- Update output/help text where needed
- Smoke check with `uv run --python 3.14 python src/main.py --help`
- Run `./scripts/verify.sh`

Copy/Paste Template:

```text
Task: Add/modify CLI command <command>

Constraints:
- Preserve no-arg TUI behavior
- Preserve current broker selection semantics unless requested

Done when:
- --help reflects command
- ./scripts/verify.sh passes
```

## 4) TUI Change Safety Checklist

Goal: modify terminal UI without breaking event-loop responsiveness.

Checklist:
- Edit only required TUI modules (`src/tui/*.py`)
- Avoid blocking calls (`time.sleep`, blocking waits) in UI flow
- Preserve message/response handling behavior
- Verify redraw/update hooks still fire in expected flows
- Run `./scripts/verify.sh`

Copy/Paste Template:

```text
Task: Update TUI behavior <feature>

Must not do:
- No blocking sleeps/waits in UI path
- No unrelated refactor in input handling

Done when:
- Interaction remains responsive
- ./scripts/verify.sh passes
```

## 5) Safe Documentation-Only Change

Goal: improve docs without changing runtime behavior.

Checklist:
- Edit only documentation files
- Keep commands consistent with runbook (`uv run --python 3.14 ...`)
- Ensure no secret-bearing examples

Copy/Paste Template:

```text
Task: Update docs for <topic>

Constraints:
- No code changes
- No credential examples with real values

Done when:
- Commands are accurate and copy/paste safe
```
