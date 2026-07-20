"""Shared test fixtures and path setup for hzsz_iot_001 tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Allow importing the integration either as a Home Assistant custom component
# (custom_components.hzsz_iot_001) or directly from this repository layout.
_here = Path(__file__).parent.parent
_parent = _here.parent

try:
    import custom_components.hzsz_iot_001  # noqa: F401
except ImportError:
    sys.path.insert(0, str(_parent))
