"""Shared test fixtures.

The per-minute request windows are process-wide state, exactly like the
in-flight counters next to them: they protect one worker process, so they
live for as long as the process does. A test suite is one process, so a test
that fakes several upstream turns would otherwise inherit the budget spent by
the test before it and refuse its own request with a 429 that has nothing to
do with what it is testing.

The windows are therefore cleared around every test. The behaviour itself is
covered on purpose in tests/test_rate_discipline.py, which drives the window
explicitly.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_rate_windows():
    functions._rate_windows.clear()
    yield
    functions._rate_windows.clear()
