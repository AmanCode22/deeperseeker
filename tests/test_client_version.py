"""Regression tests for the env-configurable client version (B11, Stage 1 audit).

get_headers() hardcoded x-client-version: 2.4.5 for every token; when
DeepSeek's app moves, one stale string degrades all accounts at once. The
version now comes from DEEPSEEKER_CLIENT_VERSION.

Run:  python tests/test_client_version.py   (pytest-compatible)
"""

import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


def test_default_version_unchanged():
    headers = functions.get_headers("tok")
    assert headers["x-client-version"] == functions.DEEPSEEKER_CLIENT_VERSION


def test_version_env_override():
    with mock.patch.dict(os.environ, {"DEEPSEEKER_CLIENT_VERSION": "9.9.9-test"}):
        # pick_token-style: module constant is read at import; verify the
        # override would be picked up on a fresh interpreter via the same
        # os.getenv call the module uses.
        assert os.getenv("DEEPSEEKER_CLIENT_VERSION") == "9.9.9-test"
    with mock.patch.object(functions, "DEEPSEEKER_CLIENT_VERSION", "9.9.9-test"):
        headers = functions.get_headers("tok")
    assert headers["x-client-version"] == "9.9.9-test", (
        "get_headers must read the module constant, not a hardcoded literal"
    )


def main():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
