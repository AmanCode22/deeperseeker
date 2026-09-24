"""Regression test: every streamed chat.completions chunk carries `index`.

Strict clients validate each SSE frame against OpenAI's schema. Vercel's AI SDK
(used by Trilium Notes) and Zed both reject frames whose `choices[]` entries are
missing the `index` field, e.g.:

    Type validation failed: Value: {"choices":[{"delta":{},"finish_reason":"stop"}]}
    expected "number" at path ["choices",0,"index"]

`stream_response` used to emit bare `{"choices":[{"delta":...}]}` frames. This
test locks in that all chunks now include `index` (plus id/object/created).

Run with pytest, or directly: python tests/test_chat_chunk_index.py
"""
import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


async def _collect(agen):
    return [line async for line in agen]


async def _gen(chunks):
    for c in chunks:
        yield c


def _data_payloads(lines):
    payloads = []
    for line in lines:
        if not line.startswith("data: "):
            continue
        body = line[len("data: "):].strip()
        if body == "[DONE]":
            continue
        payloads.append(json.loads(body))
    return payloads


MESSAGES = [{"role": "user", "content": "hello there"}]
CHUNKS = ["Hel", "lo ", "world"]


def _run_stream():
    with mock.patch.multiple(
        app_module,
        mark_active=mock.DEFAULT,
        save_session=mock.DEFAULT,
        generate_signature_sync=mock.DEFAULT,
    ):
        return asyncio.run(
            _collect(app_module.stream_response(
                _gen(CHUNKS), "v4.1flash", MESSAGES, 1, "sess", "sig", []
            ))
        )


def test_every_choice_has_index():
    lines = _run_stream()
    payloads = _data_payloads(lines)
    assert payloads, "expected streamed chunks"

    for p in payloads:
        for c in p.get("choices", []):
            assert "index" in c, f"choice missing index: {p}"
            assert isinstance(c["index"], int), f"index must be int: {p}"
            assert "finish_reason" in c, f"choice missing finish_reason: {p}"

    # Content deltas that carry text must have an index too.
    text_chunks = [
        p for p in payloads
        if p.get("choices") and c_get(p["choices"][0]["delta"], "content")
    ]
    assert text_chunks, f"expected text deltas, got {payloads}"


def test_chunk_envelope_fields():
    lines = _run_stream()
    payloads = _data_payloads(lines)
    # Only choices-bearing chunks carry the envelope. The trailing usage chunk
    # intentionally uses an empty choices array (see test_stream_usage.py) and is
    # left untouched by this change.
    for p in payloads:
        if not p.get("choices"):
            continue
        assert p.get("object") == "chat.completion.chunk", p
        assert isinstance(p.get("id"), str) and p["id"], p
        assert isinstance(p.get("created"), int), p


def c_get(d, k):
    return d.get(k) if isinstance(d, dict) else None


def main():
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
