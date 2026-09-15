"""Regression tests for the OpenAI streaming usage chunk.

OpenAI-compatible gateways (New API, sub2api, ...) bill from the `usage`
field of the streaming response.  The other three response paths already
carry it; only `stream_response` (OpenAI chat completions, streaming) used
to omit it, which showed up as `0/0` tokens on the pool dashboard.

The fix emits a dedicated usage chunk with an empty `choices` array right
before `[DONE]`.  `choices: []` is required: sub2api only records usage
chunks that have an empty choices array (isOpenAIChatUsageOnlyStreamChunk).

Run with pytest, or directly: python tests/test_stream_usage.py
"""
import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _stub_side_effects():
    """Keep stream_response off the DB / session cache while testing."""
    return mock.patch.multiple(
        app_module,
        mark_active=mock.DEFAULT,
        save_session=mock.DEFAULT,
        generate_signature_sync=mock.DEFAULT,
    )


async def _collect(agen):
    out = []
    async for line in agen:
        out.append(line)
    return out


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


async def _gen(chunks, fail_after=None):
    for i, c in enumerate(chunks):
        if fail_after is not None and i == fail_after:
            raise RuntimeError("upstream exploded")
        yield c


MESSAGES = [{"role": "user", "content": "hello there"}]
CHUNKS = ["Hel", "lo ", "world"]


def _run_stream(**kwargs):
    with mock.patch.multiple(
        app_module,
        mark_active=mock.DEFAULT,
        save_session=mock.DEFAULT,
        generate_signature_sync=mock.DEFAULT,
    ):
        return asyncio.run(
            _collect(app_module.stream_response(
                _gen(CHUNKS, **kwargs), "v4.1flash", MESSAGES, 1, "sess", "sig", []
            ))
        )


def test_usage_chunk_emitted_before_done():
    lines = _run_stream()

    assert lines[-1] == "data: [DONE]\n\n", lines[-1]
    payloads = _data_payloads(lines)
    usage_chunks = [p for p in payloads if "usage" in p]
    assert len(usage_chunks) == 1, f"expected exactly one usage chunk, got {payloads}"

    usage_chunk = usage_chunks[0]
    # sub2api only records usage chunks whose choices array is empty.
    assert usage_chunk["choices"] == [], usage_chunk["choices"]

    usage = usage_chunk["usage"]
    expected_in = app_module.count_tok(app_module._messages_text(MESSAGES))
    expected_out = app_module.count_tok("Hello world")
    assert usage["prompt_tokens"] == expected_in, usage
    assert usage["completion_tokens"] == expected_out, usage
    assert usage["total_tokens"] == expected_in + expected_out, usage

    # The usage chunk must come after the finish_reason chunk.
    finish_idx = next(
        i for i, p in enumerate(payloads)
        if p.get("choices") and p["choices"][0].get("finish_reason")
    )
    assert payloads.index(usage_chunk) > finish_idx


def test_no_usage_chunk_when_upstream_fails():
    lines = _run_stream(fail_after=1)

    assert lines[-1] != "data: [DONE]\n\n", "failed streams must not emit [DONE]"
    payloads = _data_payloads(lines)
    assert not any("usage" in p for p in payloads), payloads
    assert any("error" in p for p in payloads), payloads


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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