"""Regression tests for the secure-default boot behavior (B9, Stage 1 audit).

The API key used to fall back to the publicly documented 'dseeker' silently;
an unset key is now generated, printed once and persisted next to the DB.
Explicit keys are honored unchanged.

Run:  python tests/test_security_defaults.py   (pytest-compatible)
"""

import os
import sys
import tempfile
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402


def _with_db_dir(tmpdir):
    """Point functions.data_dir() at tmpdir for the duration of a test.

    B12 anchors the DB (and runtime files next to it) at import time via
    functions._db, so tests patch that module attribute rather than DB_PATH."""
    saved = functions._db
    functions._db = os.path.join(tmpdir, "deeperseeker.db")

    def _restore():
        functions._db = saved

    return _restore


def test_explicit_key_honored():
    with mock.patch.dict(os.environ, {"DEEPSEEKER_API_KEY": "my-custom-key"}):
        key, generated = app_module._resolve_api_key()
    assert key == "my-custom-key"
    assert generated is False


def test_empty_key_generates_and_persists():
    tmpdir = tempfile.mkdtemp()
    key_file = os.path.join(tmpdir, "api_key.txt")
    restore = _with_db_dir(tmpdir)
    try:
        with mock.patch.dict(os.environ, {"DEEPSEEKER_API_KEY": ""}):
            key1, generated1 = app_module._resolve_api_key()
            assert generated1 is True
            assert key1.startswith("dsk-") and len(key1) > 20, key1
            # persisted with restrictive permissions
            assert os.path.isfile(key_file)
            with open(key_file) as f:
                assert f.read().strip() == key1
            if os.name == "posix":
                assert not (os.stat(key_file).st_mode & 0o077), (
                    "key file must not be group/world accessible"
                )
            # a second boot reuses the persisted key instead of rotating it
            key2, generated2 = app_module._resolve_api_key()
    finally:
        restore()
    assert key2 == key1, "the persisted key must survive restarts"
    assert generated2 is True  # still "generated" (not user-supplied), but stable


def test_generated_key_never_equals_documented_default():
    tmpdir = tempfile.mkdtemp()
    restore = _with_db_dir(tmpdir)
    try:
        with mock.patch.dict(os.environ, {"DEEPSEEKER_API_KEY": ""}):
            key, _ = app_module._resolve_api_key()
    finally:
        restore()
    assert key != "dseeker"


def test_banner_classifies_loopback_and_exposed_hosts():
    # Must not raise, and must pick the exposed-host branch when HOST != loopback.
    with mock.patch.dict(os.environ, {"HOST": "0.0.0.0"}):
        app_module._log_security_banner()  # smoke: logs, never crashes
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1"}):
        app_module._log_security_banner()


def test_module_api_key_always_present():
    assert app_module.API_KEY, "API_KEY must never be empty (fail-open)"


# ==============================================================================
# P0-8 (Days 15-16): exposure policy — default credentials never meet a
# non-loopback bind. Boot matrix before the fix:
#   Boot 1: no key set                       -> generated key, shown once (B9)   [ok]
#   Boot 2: admin/admin + loopback bind      -> loud warning, boots              [ok]
#   Boot 3: admin/admin + non-loopback bind  -> loud warning, STILL BOOTS        [gap]
# After the fix Boot 3 refuses to start, naming the exact fix.
# ==============================================================================


def test_boot3_default_admin_non_loopback_refuses():
    """Defaults meeting a non-loopback bind must refuse to start, and the
    refusal must name the exact env fix."""
    with mock.patch.dict(
        os.environ,
        {"HOST": "0.0.0.0"},
    ):
        try:
            app_module._enforce_exposure_policy()
        except SystemExit as e:
            message = str(e)
            assert "DEEPSEEKER_ADMIN_PASSWORD" in message, (
                "the refusal must name the exact fix"
            )
            assert "DEEPSEEKER_ADMIN_USER" in message
        else:
            raise AssertionError(
                "default admin credentials on a non-loopback bind must refuse to start"
            )


def test_boot2_default_admin_loopback_still_boots():
    """Loopback + defaults keeps the landed B9 behavior: warn, don't refuse."""
    with mock.patch.dict(os.environ, {"HOST": "127.0.0.1"}):
        app_module._enforce_exposure_policy()  # must not raise


def test_boot2_default_admin_unset_host_still_boots():
    """HOST unset means the 127.0.0.1 default — that is a loopback boot."""
    with mock.patch.dict(os.environ, {}, clear=True):
        app_module._enforce_exposure_policy()  # must not raise


def test_non_default_admin_non_loopback_boots():
    """Explicit credentials plus an exposed bind is a legitimate deployment."""
    with (
        mock.patch.dict(os.environ, {"HOST": "0.0.0.0"}),
        mock.patch.object(app_module, "ADMIN_USER", "ops"),
        mock.patch.object(app_module, "ADMIN_PASSWORD", "correct horse battery staple"),
    ):
        app_module._enforce_exposure_policy()  # must not raise


def test_boot3_escape_hatch_is_explicit():
    """The documented override must exist and must be opt-in by name."""
    with mock.patch.dict(
        os.environ, {"HOST": "0.0.0.0", "DEEPSEEKER_ALLOW_DEFAULT_ADMIN": "1"}
    ):
        app_module._enforce_exposure_policy()  # explicit acceptance -> boots
    with mock.patch.dict(
        os.environ, {"HOST": "0.0.0.0", "DEEPSEEKER_ALLOW_DEFAULT_ADMIN": "0"}
    ):
        try:
            app_module._enforce_exposure_policy()
        except SystemExit:
            pass
        else:
            raise AssertionError("any value other than '1' must keep the refusal")


TESTS = [
    test_boot3_default_admin_non_loopback_refuses,
    test_boot2_default_admin_loopback_still_boots,
    test_boot2_default_admin_unset_host_still_boots,
    test_non_default_admin_non_loopback_boots,
    test_boot3_escape_hatch_is_explicit,
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
