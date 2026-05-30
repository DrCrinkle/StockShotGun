from __future__ import annotations

import sys
from pathlib import Path

# Put `src` on the path so top-level test modules import the same way the
# tests/agentic and tests/enforcement conftests already arrange for their dirs.
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
