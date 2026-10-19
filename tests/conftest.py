from __future__ import annotations

import sys
from pathlib import Path

# Allow `pytest` before `pip install -e .` (src layout).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
