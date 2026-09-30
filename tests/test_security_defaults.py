"""Regression tests for the secure-default boot behavior (B9, Stage 1 audit).

The API key used to fall back to the publicly documented 'dseeker' silently;
an unset key is now generated, printed once and persisted next to the DB.
Explicit keys are honored unchanged.

Run:  python tests/test_security_defaults.py   (pytest-compatible)
"""
import builtins
import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def test_explicit_key_honored():
    with mock.patch.dict(os.environ, {"DEEPSEEKER_API_KEY": "my-custom-key"}):
        key, generated = app_module._resolve_api_key()
    assert key == "my-custom-key"
    assert generated is False


def test_empty_key_generates_and_persists():
    tmpdir = tempfile.mkdtemp()
    key_file = os.path.join(tmpdir, "api_key.txt")
    env = {"DEEPSEEKER_API_KEY": "", "DB_PATH": os.path.join(tmpdir, "deeperseeker.db")}
    with mock.patch.dict(os.environ, env):
        key1, generated1 = app_module._resolve_api_key()
        assert generated1 is True
        assert key1.startswith("dsk-") and len(key1) > 20, key1
        # persisted with restrictive permissions
        assert os.path.isfile(key_file)
        with open(key_file) as f:
            assert f.read().strip() == key1
        if os.name == "posix":
            assert not (os.stat(key_file).st_mode & 0o077), "key file must not be group/world accessible"
        # a second boot reuses the persisted key instead of rotating it
        key2, generated2 = app_module._resolve_api_key()
    assert key2 == key1, "the persisted key must survive restarts"
    assert generated2 is True  # still "generated" (not user-supplied), but stable


def test_generated_key_never_equals_documented_default():
    tmpdir = tempfile.mkdtemp()
    env = {"DEEPSEEKER_API_KEY": "", "DB_PATH": os.path.join(tmpdir, "deeperseeker.db")}
    with mock.patch.dict(os.environ, env):
        key, _ = app_module._resolve_api_key()
    assert key != "dseeker"


def test_banner_classifies_loopback_and_exposed_hosts():
    # Must not raise, and must pick the exposed-host branch when HOST != loopback.
    with mock.patch.dict(os.environ, {"HOST": "0.0.0.0"}):
        app_module._log_security_banner()  # smoke: logs, never crashes
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1"}):
        app_module._log_security_banner()


def test_module_api_key_always_present():
    assert app_module.API_KEY, "API_KEY must never be empty (fail-open)"


TESTS = [
    test_explicit_key_honored,
    test_empty_key_generates_and_persists,
    test_generated_key_never_equals_documented_default,
    test_banner_classifies_loopback_and_exposed_hosts,
    test_module_api_key_always_present,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
