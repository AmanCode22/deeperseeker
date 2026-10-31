"""Tests for the upstream-hardening patches.

Covers:
  * /v1/messages/count_tokens (was 404 — every Claude Code count probe)
  * functions._biz_data / UpstreamBizError (HTTP 200 + null biz_data used to
    die with TypeError inside the route; the token never rotated)
  * app._with_deadline (no upper bound on one upstream turn)
  * app._is_truncated_stream_error (upstream hang-up mid-stream)

Run:  python tests/test_upstream_hardening.py   (pytest-compatible)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp  # noqa: E402

import app as app_module  # noqa: E402
from functions import UpstreamBizError, UpstreamError, _biz_data  # noqa: E402


# --------------------------------------------------------------------------
# count_tokens route
# --------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(app_module.app)


def test_count_tokens_requires_api_key():
    r = _client().post("/v1/messages/count_tokens", json={"messages": []})
    assert r.status_code == 401, r.status_code


def test_count_tokens_counts_messages_and_string_system():
    r = _client().post(
        "/v1/messages/count_tokens",
        headers={"x-api-key": app_module.API_KEY},
        json={
            "model": "claude-sonnet-4",
            "system": "you are terse",
            "messages": [{"role": "user", "content": "hello world"}],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"input_tokens"}, body
    assert body["input_tokens"] > 0


def test_count_tokens_flattens_list_system_and_content_blocks():
    r = _client().post(
        "/v1/messages/count_tokens",
        headers={"x-api-key": app_module.API_KEY},
        json={
            "system": [{"type": "text", "text": "abc def"}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "ghi"},
                        {"type": "image", "source": {}},
                    ],
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["input_tokens"] > 0


def test_count_tokens_matches_tokenizer_on_flattened_text():
    body = {
        "system": "sys",
        "messages": [{"role": "user", "content": "one two three"}],
    }
    r = _client().post(
        "/v1/messages/count_tokens",
        headers={"authorization": "Bearer " + app_module.API_KEY},
        json=body,
    )
    expected = app_module.count_tok("sys\none two three")
    assert r.json()["input_tokens"] == expected


# --------------------------------------------------------------------------
# _biz_data
# --------------------------------------------------------------------------


def test_biz_data_returns_biz_data_when_present():
    payload = {"code": 0, "data": {"biz_data": {"chat_session": {"id": "s1"}}}}
    assert _biz_data(payload) == {"chat_session": {"id": "s1"}}


def test_biz_data_null_data_raises_and_maps_auth_to_401():
    payload = {"code": 40003, "msg": "user login expired", "data": None}
    try:
        _biz_data(payload, "chat_session/create")
    except UpstreamBizError as e:
        assert e.status == 401, e.status
        assert isinstance(e, UpstreamError)  # rotation path understands it
        assert "chat_session/create" in str(e)
    else:
        raise AssertionError("expected UpstreamBizError")


def test_biz_data_null_biz_data_maps_non_auth_to_429():
    payload = {"code": 42900, "msg": "messages too frequent", "data": {"biz_data": None}}
    try:
        _biz_data(payload, "chat/create_pow_challenge")
    except UpstreamBizError as e:
        assert e.status == 429, e.status
    else:
        raise AssertionError("expected UpstreamBizError")


def test_biz_data_non_dict_payload_is_an_error_not_a_crash():
    for payload in (None, [], "oops", 0):
        try:
            _biz_data(payload)
        except UpstreamBizError:
            pass
        else:
            raise AssertionError(f"expected UpstreamBizError for {payload!r}")


def test_upstream_biz_error_status_is_seen_by_the_rotation_path():
    e = UpstreamBizError(401, "x")
    assert app_module._upstream_http_code(e) == 401


# --------------------------------------------------------------------------
# _with_deadline
# --------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


async def _fast_gen(n=3):
    for i in range(n):
        yield f"c{i}"


async def _slow_gen():
    for _ in range(100):
        await asyncio.sleep(0.5)
        yield "late"


def test_with_deadline_passes_chunks_through_unchanged():
    async def go():
        out = []
        async for chunk in app_module._with_deadline(_fast_gen(), "t", 5):
            out.append(chunk)
        return out

    assert _run(go()) == ["c0", "c1", "c2"]


def test_with_deadline_raises_upstream_504_on_hang():
    async def go():
        async for _ in app_module._with_deadline(_slow_gen(), "t", 0.2):
            pass

    try:
        _run(go())
    except UpstreamError as e:
        assert e.status == 504, e.status
    else:
        raise AssertionError("expected UpstreamError(504)")


def test_with_deadline_does_not_deadlock_on_early_consumer_abort():
    async def go():
        gen = app_module._with_deadline(_slow_gen(), "t", 5)
        first = await gen.__anext__()
        assert first == "late"
        await asyncio.wait_for(gen.aclose(), timeout=2)

    _run(go())


def test_with_deadline_cancels_upstream_generator_on_abort():
    closed = {"v": False}

    async def tracked():
        try:
            while True:
                await asyncio.sleep(0.5)
                yield "x"
        finally:
            closed["v"] = True

    async def go():
        gen = app_module._with_deadline(tracked(), "t", 5)
        await gen.__anext__()
        await asyncio.wait_for(gen.aclose(), timeout=2)

    _run(go())
    assert closed["v"] is True


# --------------------------------------------------------------------------
# _is_truncated_stream_error
# --------------------------------------------------------------------------


def test_truncated_stream_error_detects_aiohttp_payload_error():
    assert app_module._is_truncated_stream_error(aiohttp.ClientPayloadError("x"))
    assert app_module._is_truncated_stream_error(aiohttp.ClientError("x"))


def test_truncated_stream_error_excludes_timeouts_and_other_errors():
    assert not app_module._is_truncated_stream_error(TimeoutError())
    assert not app_module._is_truncated_stream_error(RuntimeError("boom"))
    assert not app_module._is_truncated_stream_error(UpstreamError(429, "x"))


TESTS = [
    test_count_tokens_requires_api_key,
    test_count_tokens_counts_messages_and_string_system,
    test_count_tokens_flattens_list_system_and_content_blocks,
    test_count_tokens_matches_tokenizer_on_flattened_text,
    test_biz_data_returns_biz_data_when_present,
    test_biz_data_null_data_raises_and_maps_auth_to_401,
    test_biz_data_null_biz_data_maps_non_auth_to_429,
    test_biz_data_non_dict_payload_is_an_error_not_a_crash,
    test_upstream_biz_error_status_is_seen_by_the_rotation_path,
    test_with_deadline_passes_chunks_through_unchanged,
    test_with_deadline_raises_upstream_504_on_hang,
    test_with_deadline_does_not_deadlock_on_early_consumer_abort,
    test_with_deadline_cancels_upstream_generator_on_abort,
    test_truncated_stream_error_detects_aiohttp_payload_error,
    test_truncated_stream_error_excludes_timeouts_and_other_errors,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()