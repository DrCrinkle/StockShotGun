"""ADR 0006 — the neutral execution/ and enforcement/ modules must not import agentic/.

Step 2 moved the broker runtime (`in_process`), the broker contract (`ports`),
and observability (`telemetry`) into `execution/`. Step 7 relocated the
`ExecutionEngine` class body itself into `execution/engine.py`, completing the
import-direction flip (`agentic/` -> `execution/` -> `enforcement/`, never the
reverse). This test locks that win: if someone reintroduces an `import agentic`
into any of these modules, the wrong-direction dependency is caught here rather
than silently inverting the layering.

A second assertion sweeps every module under `enforcement/` for the same
violation — ADR 0006 step 6's full-package layering lock.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1] / "src"
EXEC_DIR = ROOT / "execution"
ENFORCEMENT_DIR = ROOT / "enforcement"
_AGENTIC_IMPORT = re.compile(r"^\s*(?:from|import)\s+agentic\b", re.MULTILINE)


def _find_agentic_imports(path: pathlib.Path) -> list[str]:
    src = path.read_text(encoding="utf-8")
    offenders = []
    for m in _AGENTIC_IMPORT.finditer(src):
        line = src[m.start() : src.find("\n", m.start())].strip()
        offenders.append(f"{path.name}: {line}")
    return offenders


def test_neutral_execution_modules_do_not_import_agentic():
    # Glob, not an allowlist (final-review M2): EVERY module under
    # execution/ — including files added after this test was written — must
    # stay agentic-free, or the layering silently inverts for the new file.
    paths = sorted(EXEC_DIR.glob("*.py"))
    assert paths, f"no modules found under {EXEC_DIR}"
    offenders: list[str] = []
    for path in paths:
        offenders.extend(_find_agentic_imports(path))
    assert offenders == [], f"execution/ must not import agentic/: {offenders}"


def test_enforcement_modules_do_not_import_agentic():
    offenders: list[str] = []
    for path in sorted(ENFORCEMENT_DIR.glob("*.py")):
        offenders.extend(_find_agentic_imports(path))
    assert offenders == [], f"enforcement/ must not import agentic/: {offenders}"
