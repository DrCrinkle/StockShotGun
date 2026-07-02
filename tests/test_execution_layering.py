"""ADR 0006 — the neutral execution/ modules must not import agentic/.

Step 2 moved the broker runtime (`in_process`), the broker contract (`ports`),
and observability (`telemetry`) into `execution/`. This test locks that win: if
someone reintroduces an `import agentic` into any of them, the wrong-direction
dependency is caught here rather than silently inverting the layering.

`execution/engine.py` is intentionally EXCLUDED: in step 2 it still re-exports
`ExecutionEngine` from `agentic/router/_server.py` (the engine body hasn't moved
yet). When the engine body relocates, add it to AGENTIC_FREE and this test
becomes the step-6 layering lock for the whole package.
"""

from __future__ import annotations

import pathlib
import re

EXEC_DIR = pathlib.Path(__file__).resolve().parents[1] / "src" / "execution"
AGENTIC_FREE = ("telemetry.py", "in_process.py", "ports.py")
_AGENTIC_IMPORT = re.compile(r"^\s*(?:from|import)\s+agentic\b", re.MULTILINE)


def test_neutral_execution_modules_do_not_import_agentic():
    offenders: list[str] = []
    for name in AGENTIC_FREE:
        src = (EXEC_DIR / name).read_text(encoding="utf-8")
        for m in _AGENTIC_IMPORT.finditer(src):
            line = src[m.start() : src.find("\n", m.start())].strip()
            offenders.append(f"{name}: {line}")
    assert offenders == [], f"execution/ must not import agentic/: {offenders}"
