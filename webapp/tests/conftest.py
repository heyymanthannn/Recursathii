"""Pytest bootstrap: pin the suite to a deterministic clock (tests/clock.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clock  # noqa: E402,F401  (pins time.time on import)
