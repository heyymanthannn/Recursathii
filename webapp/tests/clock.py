"""Deterministic clock for the RecurSaathi test suite.

The permission engine branches on quiet hours (21:00-08:00 Asia/Kolkata), so
suites that exercise the send path behave differently depending on the
wall-clock time they run at. This module pins time.time to a fixed IST time of
day that still advances at the real rate, making every run deterministic.
Default: 12:30 IST (outside quiet hours). Tests that need the quiet state call
set(23, 30) and then reset().

Importing this module is idempotent: the patch installs once per process.
"""
import datetime as _dt
import time as _time

_IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _ts(h, m):
    return _dt.datetime(2026, 11, 13, h, m, tzinfo=_IST).timestamp()


_state = {"base": _ts(12, 30), "offset": 0.0}
_real = _time.time
_start = _real()


def _pinned():
    return _state["base"] + _state["offset"] + (_real() - _start)


if not getattr(_time.time, "_rs_pinned", False):
    _pinned._rs_pinned = True
    _time.time = _pinned


def set(hour_ist, minute_ist=0):
    """Shift the pinned clock to the given IST time of day (rate unchanged)."""
    _state["offset"] = _ts(hour_ist, minute_ist) - _state["base"]


def reset():
    _state["offset"] = 0.0
