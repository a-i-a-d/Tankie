"""pytest path setup for the brain tests (issue #72).

The brain package lives at ``raspberry_pi/brain`` (D10). Add
``raspberry_pi/`` to ``sys.path`` so ``import brain`` resolves when pytest
is run from the repo root (``pytest tests/brain/ -q``).
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PI_DIR = _REPO_ROOT / "raspberry_pi"
if str(_PI_DIR) not in sys.path:
    sys.path.insert(0, str(_PI_DIR))
