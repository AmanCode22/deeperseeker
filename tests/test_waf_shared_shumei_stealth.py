import asyncio
from unittest import mock

import functions


def test_waf_shared_across_tokens_while_shumei_isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_shared_waf.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    token_a = "token-alpha-111"
    token_b = "token-beta-222"

    functions.add_token(
        token_a,
        alias="alpha",
        shumei_id="Balpha_shumei_123",
        device_id="dev-uuid-alpha",
    )
    functions.add_token(
        token_b,
        alias="beta",
        shumei_id="Bbeta_shumei_456",
        device_id="dev-uuid-beta",
    )

    shared_waf_cookie = {
        "aws-waf-token": "shared-waf-token-999",
        "ds_cookie_preference": "%257B%2522level%2522%253A%2522all%2522%257D",
    }
    monkeypatch.setattr(
        functions,
        "get_cookies_safely",
        mock.AsyncMock(return_value=shared_waf_cookie),
    )

    ca, sha, da = asyncio.run(functions.get_isolated_cookies_for_token(token_a))
    cb, shb, db = asyncio.run(functions.get_isolated_cookies_for_token(token_b))

    assert ca["aws-waf-token"] == "shared-waf-token-999"
    assert cb["aws-waf-token"] == "shared-waf-token-999"

    assert sha == "Balpha_shumei_123"
    assert shb == "Bbeta_shumei_456"
    assert sha != shb

    assert da == "dev-uuid-alpha"
    assert db == "dev-uuid-beta"
    assert da != db


def test_warmup_always_refreshes_tokens_when_fast_start_disabled(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_warmup_fast.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    for i in range(5):
        functions.add_token(
            f"token-{i}",
            alias=f"tok-{i}",
            shumei_id=f"Bshumei_{i}",
            device_id=f"dev-id-{i}",
        )

    monkeypatch.setattr(functions, "_read_cookie_file", lambda: {"aws-waf-token": "waf"})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("DEEPSEEKER_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("DEEPSEEKER_FAST_START", raising=False)

    batch_mock = mock.AsyncMock()
    monkeypatch.setattr(functions, "_generate_shumei_ids_for_tokens", batch_mock)

    asyncio.run(functions.warmup_all_tokens_cookies())

    batch_mock.assert_called_once()
    called_tokens = batch_mock.call_args[0][0]
    assert len(called_tokens) == 5


def test_warmup_batches_all_tokens_even_if_some_have_shumei(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_warmup_batch.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    functions.add_token("tok-ready", alias="ready", shumei_id="Bready123")
    functions.add_token("tok-needs-1", alias="need1")
    functions.add_token("tok-needs-2", alias="need2")

    monkeypatch.setattr(functions, "_read_cookie_file", lambda: {"aws-waf-token": "waf"})
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("DEEPSEEKER_DISABLE_WARMUP", raising=False)
    monkeypatch.delenv("DEEPSEEKER_FAST_START", raising=False)

    batch_mock = mock.AsyncMock()
    monkeypatch.setattr(functions, "_generate_shumei_ids_for_tokens", batch_mock)

    asyncio.run(functions.warmup_all_tokens_cookies())

    batch_mock.assert_called_once()
    called_tokens = batch_mock.call_args[0][0]
    assert len(called_tokens) == 3
    token_strs = [t[1] for t in called_tokens]
    assert "tok-needs-1" in token_strs
    assert "tok-needs-2" in token_strs
    assert "tok-ready" in token_strs


def test_stealth_evasion_configuration():
    assert "--disable-blink-features=AutomationControlled" in functions._CHROMIUM_STEALTH_ARGS
    assert "--no-sandbox" in functions._CHROMIUM_STEALTH_ARGS
    assert "--disable-dev-shm-usage" in functions._CHROMIUM_STEALTH_ARGS
    assert "--disable-background-timer-throttling" in functions._CHROMIUM_STEALTH_ARGS
    assert "--disable-renderer-backgrounding" in functions._CHROMIUM_STEALTH_ARGS

    assert functions.async_playwright is not None
    assert "patchright" in functions.async_playwright.__module__

    js = functions._STEALTH_JS
    assert "webdriver" in js
    assert "window.chrome" in js
    assert "plugins" in js
    assert "permissions.query" in js
    assert "Google Inc. (NVIDIA)" in js
    assert "ANGLE (NVIDIA" in js
    assert "makeNative" in js
    assert "hardwareConcurrency" in js
    assert "deviceMemory" in js
    assert "connection" in js
    assert "[native code]" in js


def test_auto_mode_mock_default_false(monkeypatch):
    import app

    monkeypatch.delenv("DEEPSEEKER_AUTO_MODE_MOCK", raising=False)
    assert app.is_auto_mode_mock_enabled() is False

    monkeypatch.setenv("DEEPSEEKER_AUTO_MODE_MOCK", "false")
    assert app.is_auto_mode_mock_enabled() is False

    monkeypatch.setenv("DEEPSEEKER_AUTO_MODE_MOCK", "true")
    assert app.is_auto_mode_mock_enabled() is True


def test_fast_start_applies_shared_shumei_id(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_fast_start.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    functions.add_token("tok-1", alias="one", shumei_id="Bold_1")
    functions.add_token("tok-2", alias="two", shumei_id="Bold_2")

    monkeypatch.setenv("DEEPSEEKER_FAST_START", "true")
    monkeypatch.setattr(
        functions,
        "_generate_cookies",
        mock.AsyncMock(return_value=({"aws-waf-token": "waf-fresh"}, 1799999999, "Bshared_shumei_fast")),
    )
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.delenv("DEEPSEEKER_DISABLE_WARMUP", raising=False)

    batch_mock = mock.AsyncMock()
    monkeypatch.setattr(functions, "_generate_shumei_ids_for_tokens", batch_mock)

    asyncio.run(functions.warmup_all_tokens_cookies())

    batch_mock.assert_not_called()

    t1 = functions.get_token(1)
    t2 = functions.get_token(2)
    assert t1["shumei_id"] == "Bshared_shumei_fast"
    assert t2["shumei_id"] == "Bshared_shumei_fast"
    import json
    c1 = json.loads(t1["cookies"])
    assert c1["cookie"]["aws-waf-token"] == "waf-fresh"


def test_warmup_headless_toggle(monkeypatch):
    monkeypatch.delenv("DEEPSEEKER_COOKIE_HEADLESS", raising=False)
    monkeypatch.delenv("DEEPSEEKER_WARMUP_NON_HEADLESS", raising=False)
    monkeypatch.delenv("DEEPSEEKER_NON_HEADLESS_WARMUP", raising=False)
    monkeypatch.delenv("DEEPSEEKER_WARMUP_HEADLESS", raising=False)
    assert functions.is_cookie_headless() is True

    monkeypatch.setenv("DEEPSEEKER_WARMUP_NON_HEADLESS", "true")
    assert functions.is_cookie_headless() is False

    monkeypatch.delenv("DEEPSEEKER_WARMUP_NON_HEADLESS", raising=False)
    monkeypatch.setenv("DEEPSEEKER_COOKIE_HEADLESS", "false")
    assert functions.is_cookie_headless() is False

    monkeypatch.setenv("DEEPSEEKER_COOKIE_HEADLESS", "true")
    assert functions.is_cookie_headless() is True


def test_get_isolated_cookies_non_blocking_on_missing_shumei(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test_non_blocking.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    token_str = "tok-instant-test"
    functions.add_token(token_str, alias="instant")

    monkeypatch.setattr(
        functions, "get_cookies_safely", mock.AsyncMock(return_value={"aws-waf-token": "w"})
    )
    monkeypatch.setattr(functions, "_read_global_shumei_id", lambda: "Bfallback_shumei")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    bg_mock = mock.AsyncMock()
    monkeypatch.setattr(functions, "_generate_shumei_background", bg_mock)

    c, sh, dev = asyncio.run(functions.get_isolated_cookies_for_token(token_str))

    assert c["aws-waf-token"] == "w"
    assert sh == "Bfallback_shumei"
    assert dev is not None


def test_normalize_shumei_device_id_prefix_and_unquoting():
    assert functions.normalize_shumei_device_id(None) is None
    assert functions.normalize_shumei_device_id("") is None
    assert functions.normalize_shumei_device_id("BalreadyHasB==") == "BalreadyHasB=="
    assert functions.normalize_shumei_device_id("needsB==") == "BneedsB=="
    assert functions.normalize_shumei_device_id("needsB%3D%3D") == "BneedsB=="
    assert functions.normalize_shumei_device_id("Balready%3D%3D") == "Balready=="


def test_extract_shumei_device_id_ensures_prefix_b(monkeypatch):
    cookies = {
        "aws-waf-token": "waf",
        "smidV2": "20261111...",
        ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae": "rawThumbcacheValue%3D%3D",
    }
    extracted = functions.extract_shumei_device_id(cookies)
    assert extracted == "BrawThumbcacheValue=="
    assert extracted.startswith("B")

    monkeypatch.setenv("DEEPSEEKER_SHUMEI_DEVICE_ID", "envValWithoutB==")
    assert functions.extract_shumei_device_id({}) == "BenvValWithoutB=="

    monkeypatch.setenv("DEEPSEEKER_SHUMEI_DEVICE_ID", "BenvValWithB==")
    assert functions.extract_shumei_device_id({}) == "BenvValWithB=="


def test_send_message_includes_normalized_shumei_id(monkeypatch):
    sh_id = "testShumeiWithoutB=="
    monkeypatch.setattr(
        functions,
        "get_isolated_cookies_for_token",
        mock.AsyncMock(
            return_value=(
                {
                    "aws-waf-token": "w",
                    "smidV2": "s",
                    ".thumbcache_abc": "testShumeiWithoutB%3D%3D",
                },
                sh_id,
                "dev-123",
            )
        ),
    )
    monkeypatch.setattr(functions, "solve_create_pow", mock.AsyncMock(return_value="pow-123"))

    captured_req = {}

    class FakeResp:
        status = 200
        headers = {"content-type": "text/event-stream"}

        def __init__(self):
            self.content = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_any(self):
            yield b'data: {"p": "response", "o": "BATCH", "v": [{"p": "fragments", "v": [{"type": "RESPONSE", "content": "hi"}]}]}\n\ndata: {"p": "response/status", "v": "FINISHED"}\n\n'

    async def fake_post(path, headers=None, json=None, cookies=None, **kwargs):
        captured_req["path"] = path
        captured_req["json"] = json
        captured_req["cookies"] = cookies
        return FakeResp()

    monkeypatch.setattr(functions, "post_with_failover", fake_post)

    async def run_gen():
        chunks = []
        async for c in functions.send_message(
            chat_id="chat-1",
            auth_token="tok-1",
            message="hello",
            parent_message_id=None,
        ):
            chunks.append(c)
        return chunks

    res = asyncio.run(run_gen())
    assert len(res) > 0
    assert "shumei_device_id" in captured_req["json"]
    assert captured_req["json"]["shumei_device_id"] == "BtestShumeiWithoutB=="
    assert captured_req["json"]["shumei_device_id"].startswith("B")
    assert captured_req["cookies"]["smidV2"] == "s"
    assert captured_req["cookies"][".thumbcache_abc"] == "testShumeiWithoutB%3D%3D"

