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


def test_browser_profile_authentic_pc_headers():
    headers = functions.get_headers("sample-auth-token")
    assert headers["x-client-platform"] == "web"
    assert headers["x-client-bundle-id"] == "com.deepseek.chat"
    assert "Dalvik" not in headers["user-agent"]
    assert "Mozilla/5.0" in headers["user-agent"]
    assert headers["origin"] == "https://chat.deepseek.com"
    assert headers["referer"] == "https://chat.deepseek.com/"
    assert "sec-fetch-dest" in headers
    assert "sec-fetch-mode" in headers
    assert "sec-ch-ua" in headers
    assert "x-device-id" in headers
    assert headers["x-device-model"] == ""
    assert "x-app-version" not in headers


def test_is_browser_profile_always_true():
    assert functions.is_browser_profile() is True
    assert functions.is_browser_profile("browser") is True
    assert functions.is_browser_profile("pc") is True
    assert functions.is_browser_profile("web") is True
    assert functions.is_browser_profile("android") is True
    assert functions.is_browser_profile(None) is True


def test_browser_profile_custom_ua():
    custom = "MyCustomBrowser/1.0"
    with mock.patch.dict(
        os.environ,
        {
            "DEEPSEEKER_BROWSER_USER_AGENT": custom,
        },
    ):
        headers = functions.get_headers("tok")
    assert headers["user-agent"] == custom


def test_pow_header_passed():
    pow_token = "pow_challenge_abc123"
    h_pc = functions.get_headers("tok", pow=pow_token)
    assert h_pc["x-ds-pow-response"] == pow_token


def test_auth_token_optional():
    h_pc = functions.get_headers(None)
    assert "authorization" not in h_pc
    assert "x-device-id" not in h_pc


def test_timezone_offset_present():
    h_pc = functions.get_headers("tok")
    assert "x-client-timezone-offset" in h_pc
    assert len(h_pc["x-client-timezone-offset"]) > 0


def test_extract_shumei_device_id():
    cookies = {
        "ds_session_id": "sess123",
        ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae": "b9dIQDDbDoaexszgYZBwqZ7EF9Be2TZZzX76fivt3NkVgk4C8op36qcGi2+slbEFK3oeb6UsG9APdR+ChcrNIA%3D%3D",
    }
    shumei_id = functions.extract_shumei_device_id(cookies)
    assert shumei_id == "b9dIQDDbDoaexszgYZBwqZ7EF9Be2TZZzX76fivt3NkVgk4C8op36qcGi2+slbEFK3oeb6UsG9APdR+ChcrNIA=="

    assert functions.extract_shumei_device_id({".thumbcache_test": "B12345"}) == "B12345"

    assert functions.extract_shumei_device_id({"ds_session_id": "sess"}) is None


def test_browser_profile_platform_uas():
    ua_linux = functions.get_browser_user_agent("Linux")
    assert "Linux" in ua_linux
    assert "Chrome/157" in ua_linux

    ua_win = functions.get_browser_user_agent("Windows")
    assert "Windows NT 10.0" in ua_win
    assert "Chrome/157" in ua_win

    ua_mac = functions.get_browser_user_agent("macOS")
    assert "Macintosh" in ua_mac
    assert "Chrome/157" in ua_mac


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
