import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module

MESSAGES = [{"role": "user", "content": "hi"}]
MODEL = "v4.1flash"


async def _collect(agen):
    out = []
    async for frame in agen:
        out.append(frame)
    return out


def _stream_frames(gen, opts=None):
    with mock.patch.multiple(
        app_module,
        mark_active=mock.DEFAULT,
        mark_limited=mock.DEFAULT,
        save_session=mock.DEFAULT,
        generate_signature_sync=mock.DEFAULT,
        delete_sessions_for_chat=mock.DEFAULT,
    ):
        return asyncio.run(
            _collect(
                app_module.stream_responses_response(
                    gen, MODEL, MESSAGES, 1, "sess", "sig", [], opts=opts
                )
            )
        )


def _events(frames):
    out = []
    for frame in frames:
        lines = [line for line in frame.strip().split("\n") if line]
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        name = lines[0][len("event: ") :]
        data = json.loads(lines[1][len("data: ") :])
        assert data["type"] == name
        out.append(data)
    return out


def _response_object(opts):
    return app_module._build_response_object(
        "resp_test", 1_700_000_000, MODEL, [], status="completed", opts=opts
    )


class _RateLimited(Exception):
    status = 429


def test_response_failed_hides_exception_text():
    leak = "upstream said 401 for token sk-secret-123"

    async def gen():
        yield "partial "
        raise RuntimeError(leak)

    frames = _stream_frames(gen())
    events = _events(frames)
    assert events[-1]["type"] == "response.failed"
    assert events[-1]["response"]["status"] == "failed"
    assert events[-1]["response"]["error"] == {
        "code": "server_error",
        "message": app_module.GENERIC_RESPONSE_ERROR,
    }
    blob = "".join(frames)
    assert leak not in blob
    assert "sk-secret-123" not in blob
    assert "RuntimeError" not in blob


def test_response_failed_hides_upstream_http_details():
    async def gen():
        raise _RateLimited("HTTP 429: account artyom08 is rate limited, cookie expired")
        yield ""

    frames = _stream_frames(gen())
    events = _events(frames)
    assert events[-1]["type"] == "response.failed"
    assert (
        events[-1]["response"]["error"]["message"] == app_module.GENERIC_RESPONSE_ERROR
    )
    blob = "".join(frames)
    assert "429" not in blob
    assert "cookie" not in blob
    assert "artyom08" not in blob


def test_effort_defaults_to_none_when_not_requested():
    assert _response_object({"thinking": True})["reasoning"]["effort"] is None
    assert _response_object({})["reasoning"]["effort"] is None
    assert _response_object({"thinking": False})["reasoning"]["effort"] is None


def test_effort_echoes_explicit_client_value():
    cases = [
        ({"reasoning": {"effort": "high"}}, "high"),
        ({"thinking": True, "reasoning": {"effort": "minimal"}}, "minimal"),
        ({"reasoning_effort": "low"}, "low"),
        ({"reasoning": "low"}, "low"),
        ({"effort": "HIGH"}, "high"),
    ]
    for opts, expected in cases:
        assert _response_object(opts)["reasoning"]["effort"] == expected, opts


def test_effort_drops_unknown_values():
    assert (
        _response_object({"reasoning": {"effort": "turbo"}})["reasoning"]["effort"]
        is None
    )
    assert _response_object({"reasoning_effort": "   "})["reasoning"]["effort"] is None
    assert _response_object({"effort": 7})["reasoning"]["effort"] is None


def test_reasoning_summary_still_echoed():
    reasoning = _response_object({"reasoning": {"effort": "high", "summary": "auto"}})[
        "reasoning"
    ]
    assert reasoning == {"effort": "high", "summary": "auto"}


def test_non_stream_response_effort_without_explicit_request():
    chat_result = app_module.format_response("hi", MODEL, MESSAGES)
    plain = app_module.format_responses_response(chat_result, MODEL, {"thinking": True})
    assert plain["reasoning"]["effort"] is None
    asked = app_module.format_responses_response(
        chat_result, MODEL, {"thinking": True, "reasoning": {"effort": "high"}}
    )
    assert asked["reasoning"]["effort"] == "high"


def test_stream_reports_effort_only_when_requested():
    async def plain_gen():
        yield "<think>step</think>Hello"

    events = _events(_stream_frames(plain_gen(), opts={"thinking": True}))
    assert events[0]["type"] == "response.created"
    assert events[0]["response"]["reasoning"]["effort"] is None
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["reasoning"]["effort"] is None

    async def asked_gen():
        yield "<think>step</think>Hello"

    events = _events(
        _stream_frames(
            asked_gen(), opts={"thinking": True, "reasoning": {"effort": "high"}}
        )
    )
    assert events[0]["response"]["reasoning"]["effort"] == "high"
    assert events[-1]["response"]["reasoning"]["effort"] == "high"
