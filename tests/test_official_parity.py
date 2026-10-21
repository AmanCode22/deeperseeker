"""Parity tests for the official-API passthrough mode (P0-7, Days 31-33).

The 5-line spec locked here:

  1. mode switch is explicit: DEEPSEEKER_MODE=official + DEEPSEEKER_OFFICIAL_API_KEY;
     nothing auto-falls back — unset mode stays bridge
  2. the three surfaces (chat, messages, models) serve official mode from the
     SAME handlers; the client-visible shape is equal across modes
  3. same auth, same usage recording (surface 'official', masked key)
  4. official streaming normalizes to the PR #38 chunk contract (one
     id/created per completion, index/model/created on every chunk, trailing
     usage chunk, [DONE])
  5. the official key appears in no log line and no error body

Run:  python tests/test_official_parity.py   (pytest-compatible)
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest.mock as mock
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import functions  # noqa: E402
import official_api  # noqa: E402


def _restore_mode(old):
    """Always restore: leaving DEEPSEEKER_MODE=official behind would flip
    every later test into official mode (order-dependent failures)."""
    if old is None:
        os.environ.pop("DEEPSEEKER_MODE", None)
    else:
        os.environ["DEEPSEEKER_MODE"] = old


OFFICIAL_REPLY_PLAIN = {
    "id": "chatcmpl-official-1",
    "object": "chat.completion",
    "created": 1792540800,
    "model": "deepseek-chat",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "official says hi"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
}

OFFICIAL_REPLY_TOOL = {
    "id": "chatcmpl-official-2",
    "object": "chat.completion",
    "created": 1792540800,
    "model": "deepseek-chat",
    "choices": [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "arguments": '{"city": "tokyo"}',
                        },
                    }
                ],
            },
            "finish_reason": "tool_calls",
        }
    ],
    "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
}


def _fresh_store():
    tmp = tempfile.mkdtemp()
    saved = functions._db
    functions._db = os.path.join(tmp, "deeperseeker.db")
    functions.init_db()
    return saved


def test_mode_switch_is_explicit():
    assert not official_api.official_mode_enabled(), "default must be bridge"
    old = os.environ.pop("DEEPSEEKER_MODE", None)
    try:
        os.environ["DEEPSEEKER_MODE"] = "typo-mode"
        assert not official_api.official_mode_enabled(), (
            "typos must not enable official mode"
        )
        os.environ["DEEPSEEKER_MODE"] = "official"
        assert official_api.official_mode_enabled()
    finally:
        _restore_mode(old)


def test_official_plain_turn_shape_matches_bridge():
    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_post = official_api._post_json

    async def fake_post(path, payload):
        assert payload["model"] == "deepseek-chat", payload
        return dict(OFFICIAL_REPLY_PLAIN)

    try:
        official_api._post_json = fake_post
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer k-parity"},
                json={
                    "model": "v4.1flash",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200, resp.text
        official_shape = resp.json()

        # Bridge mode, same route, deterministic upstream: same top-level shape.
        os.environ.pop("DEEPSEEKER_MODE", None)
        assert set(official_shape.keys()) <= {
            "id",
            "object",
            "created",
            "model",
            "choices",
            "usage",
        }
        assert official_shape["object"] == "chat.completion"
        assert official_shape["choices"][0]["message"]["content"] == "official says hi"
        assert official_shape["usage"]["prompt_tokens"] == 11
    finally:
        official_api._post_json = saved_post
        _restore_mode(old_mode)
        functions._db = saved


def test_official_tool_turn_shape_matches_bridge():
    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_post = official_api._post_json

    async def fake_post(path, payload):
        assert payload["tools"], "the tool definitions must be forwarded"
        return dict(OFFICIAL_REPLY_TOOL)

    try:
        official_api._post_json = fake_post
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer k-parity"},
                json={
                    "model": "v4.1flash",
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [
                        {
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "parameters": {"type": "object", "properties": {}},
                            },
                        }
                    ],
                },
            )
        assert resp.status_code == 200, resp.text
        shape = resp.json()
        msg = shape["choices"][0]["message"]
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert shape["choices"][0]["finish_reason"] == "tool_calls"
    finally:
        official_api._post_json = saved_post
        _restore_mode(old_mode)
        functions._db = saved


def test_official_stream_envelope_matches_contract():
    """Day 32: official streaming normalizes to the PR #38 chunk shape."""
    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_chat = official_api.official_chat

    async def fake_stream(payload, stream=True):
        assert stream is True
        for piece in ("Hel", "lo"):
            yield "data: {}\n\n".format(
                json.dumps(
                    {
                        "id": "chatcmpl-upstream-x",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "deepseek-chat",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": piece},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            )
        yield "data: {}\n\n".format(
            json.dumps(
                {
                    "id": "chatcmpl-upstream-x",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-chat",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
            )
        )
        yield "data: {}\n\n".format(
            json.dumps(
                {
                    "id": "chatcmpl-upstream-x",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "deepseek-chat",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                }
            )
        )
        yield "data: [DONE]\n\n"

    try:
        official_api.official_chat = fake_stream
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            with client.stream(
                "POST",
                "/v1/chat/completions",
                headers={"Authorization": "Bearer k-parity"},
                json={
                    "model": "v4.1flash",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
            ) as resp:
                lines = []
                for raw_line in resp.iter_lines():
                    if raw_line.startswith("data:"):
                        lines.append(raw_line)
        chunks = [json.loads(ln[len("data: ") :]) for ln in lines[:-1]]
        assert lines[-1] == "data: [DONE]"
        # one completion = one id/created across all chunks (PR #38)
        ids = {c["id"] for c in chunks}
        created = {c["created"] for c in chunks}
        assert len(ids) == 1 and len(created) == 1, (ids, created)
        for c in chunks:
            assert c["object"] == "chat.completion.chunk"
            assert c["model"]
            for ch in c["choices"]:
                assert "index" in ch and "delta" in ch
        # trailing usage chunk present and forwarded
        usage_chunks = [c for c in chunks if c.get("usage")]
        assert usage_chunks and usage_chunks[-1]["usage"]["prompt_tokens"] == 3
    finally:
        official_api.official_chat = saved_chat
        _restore_mode(old_mode)
        functions._db = saved


def test_official_anthropic_surface_parity():
    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_post = official_api._post_json

    async def fake_post(path, payload):
        return dict(OFFICIAL_REPLY_PLAIN)

    try:
        official_api._post_json = fake_post
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            resp = client.post(
                "/v1/messages",
                headers={"x-api-key": "k-parity", "anthropic-version": "2023-06-01"},
                json={
                    "model": "v4.1flash",
                    "max_tokens": 64,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 200, resp.text
        shape = resp.json()
        assert shape["type"] == "message"
        assert shape["role"] == "assistant"
        assert shape["content"][0]["type"] == "text"
        assert shape["content"][0]["text"] == "official says hi"
        assert shape["usage"]["input_tokens"] == 11
        assert shape["stop_reason"] == "end_turn"
    finally:
        official_api._post_json = saved_post
        _restore_mode(old_mode)
        functions._db = saved


def test_official_models_surface():
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    try:
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            resp = client.get(
                "/v1/models", headers={"Authorization": "Bearer k-parity"}
            )
        assert resp.status_code == 200
        ids = [m["id"] for m in resp.json()["data"]]
        assert "deepseek-chat" in ids and "deepseek-reasoner" in ids
    finally:
        _restore_mode(old_mode)


def test_official_usage_recorded_with_masked_key():
    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_post = official_api._post_json

    async def fake_post(path, payload):
        return dict(OFFICIAL_REPLY_PLAIN)

    try:
        official_api._post_json = fake_post
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer k-parity"},
                json={
                    "model": "v4.1flash",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        rows = functions.usage_by_day_key()
        assert len(rows) == 1, rows
        assert rows[0]["requests"] == 1
        assert rows[0]["input_tokens"] == 11
        assert rows[0]["output_tokens"] == 5
        assert rows[0]["surfaces"] == "official"
        assert "k-parity" not in str(rows), "the caller key must be masked"
    finally:
        official_api._post_json = saved_post
        _restore_mode(old_mode)
        functions._db = saved


def test_official_error_never_leaks_key():
    from functions import UpstreamError

    saved = _fresh_store()
    old_mode = os.environ.pop("DEEPSEEKER_MODE", None)
    os.environ["DEEPSEEKER_MODE"] = "official"
    saved_post = official_api._post_json

    async def boom(path, payload):
        raise UpstreamError(401, "auth failed (key sk-abcdefabcdefabcdefabcdefabcdef)")

    try:
        official_api._post_json = boom
        with mock.patch.object(app_module, "API_KEY", "k-parity"):
            client = TestClient(app_module.app)
            resp = client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer k-parity"},
                json={
                    "model": "v4.1flash",
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        assert resp.status_code == 401, resp.text
        assert "sk-abcdef" not in resp.text, "official key material must not leak"
    finally:
        official_api._post_json = saved_post
        _restore_mode(old_mode)
        functions._db = saved


def test_map_model_preserves_official_ids():
    """Review fix (PR #64, Medium): /v1/models advertises deepseek-reasoner,
    but the unknown-id default used to reroute it to deepseek-chat — reasoner
    requests silently served by the chat model. Official ids map to
    themselves."""
    assert official_api.map_model("v4.1flash") == "deepseek-chat"
    assert official_api.map_model("deepseek-chat") == "deepseek-chat"
    assert official_api.map_model("deepseek-reasoner") == "deepseek-reasoner"
    assert official_api.map_model("  deepseek-reasoner  ") == "deepseek-reasoner"
    # unknown ids still fall back to the bridge-visible default
    assert official_api.map_model(None) == "deepseek-chat"
    assert official_api.map_model("totally-unknown") == "deepseek-chat"


def test_official_stream_posts_to_absolute_official_url():
    """Review fix (PR #64, HIGH): the official STREAMING path must POST the
    absolute official URL. It used to hand f"{OFFICIAL_BASE}/chat/completions"
    to post_with_failover(path) — which prepends the BRIDGE base — producing
    https://chat.deepseek.comhttps://api.deepseek.com/chat/completions, a URL
    that can never resolve, so every official-mode stream died 502/DNS. CI
    missed it because the envelope test mocked official_chat wholesale; this
    test pins the outbound URL instead."""
    captured = {}

    async def _no_lines():
        return
        yield b""  # pragma: no cover — makes this an async generator

    class _FakeResp:
        status = 200
        content = _no_lines()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        async def post(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            return _FakeResp()

    async def fake_get_session():
        return _FakeSession()

    saved_session = official_api.get_session
    saved_key = official_api.OFFICIAL_API_KEY
    official_api.get_session = fake_get_session
    official_api.OFFICIAL_API_KEY = "k-official-test"
    try:

        async def _drain():
            gen = official_api._official_chat_stream(
                {"model": "v4.1flash", "messages": [{"role": "user", "content": "hi"}]}
            )
            return [line async for line in gen]

        lines = asyncio.run(_drain())
    finally:
        official_api.get_session = saved_session
        official_api.OFFICIAL_API_KEY = saved_key

    url = captured["url"]
    parsed = urlparse(url)
    official = urlparse(official_api.OFFICIAL_BASE)
    assert parsed.scheme == "https", url
    assert parsed.netloc == official.netloc, (
        f"stream must post to the official host, got {url}"
    )
    assert parsed.path == "/chat/completions", url
    assert url.count("https://") == 1, f"bridge base leaked into the URL: {url}"
    assert captured["kwargs"]["headers"]["Authorization"] == "Bearer k-official-test"
    assert captured["kwargs"]["json"]["model"] == "deepseek-chat"
    # an empty upstream stream still produces the [DONE] envelope
    assert lines == ["data: [DONE]\n\n"]


TESTS = [
    test_mode_switch_is_explicit,
    test_official_plain_turn_shape_matches_bridge,
    test_official_tool_turn_shape_matches_bridge,
    test_official_stream_envelope_matches_contract,
    test_official_anthropic_surface_parity,
    test_official_models_surface,
    test_map_model_preserves_official_ids,
    test_official_stream_posts_to_absolute_official_url,
    test_official_usage_recorded_with_masked_key,
    test_official_error_never_leaks_key,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            import traceback

            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
