"""DeepSeek's INCOMPLETE status must not leak as text or end a turn silently (2026-10-24)."""

import asyncio
import json

import pytest
from test_sse_upstream import _collect_send_message

import functions


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


def test_incomplete_during_thinking_raises():
    body = _sse(
        {"v": {"response": {"fragments": [{"type": "THINK", "content": "reading files"}]}}},
        {"p": "response/fragments/-1/content", "o": "APPEND", "v": " and more"},
        {"p": "response/status", "v": "INCOMPLETE"},
        {"o": "BATCH", "v": [{"p": "quasi_status", "v": "INCOMPLETE"}]},
    )
    with pytest.raises(functions.UpstreamError) as e:
        asyncio.run(_collect_send_message(body))
    assert "INCOMPLETE" in str(e.value)


def test_incomplete_after_answer_keeps_answer_without_status_word():
    body = _sse(
        {"v": {"response": {"fragments": [{"type": "RESPONSE", "content": "partial answer"}]}}},
        {"p": "response/status", "v": "INCOMPLETE"},
        {"o": "BATCH", "v": [{"p": "quasi_status", "v": "INCOMPLETE"}]},
    )
    out = asyncio.run(_collect_send_message(body))
    assert "partial answer" in out and "INCOMPLETE" not in out


def test_nested_status_field_is_not_content():
    body = _sse(
        {"v": {"response": {"fragments": [{"type": "RESPONSE", "content": "answer"}]}}},
        {"p": "response/fragments/-1/status", "v": "INCOMPLETE"},
    )
    out = asyncio.run(_collect_send_message(body))
    assert out.strip() == "answer"
