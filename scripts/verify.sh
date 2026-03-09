#!/usr/bin/env bash
set -euo pipefail

echo "[verify] Running static type checks"
uv run --python 3.14 mypy . --show-error-codes --pretty --ignore-missing-imports

echo "[verify] Running syntax compilation checks"
uv run --python 3.14 python -m py_compile stockshotgun src/main.py src/setup.py src/order_processor.py src/brokers/*.py src/tui/*.py

echo "[verify] Running safe smoke tests"
uv run --python 3.14 python src/main.py --help >/dev/null 2>&1
uv run --python 3.14 python stockshotgun --help >/dev/null 2>&1

echo "[verify] OK"
