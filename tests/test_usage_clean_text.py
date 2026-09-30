"""Regression tests for usage counting on the cleaned completion (B7).

format_response counted count_tok() on the RAW upstream text before <think>
reasoning and DSML/tool markup were stripped (same in the streaming usage
chunk), so completion_tokens — and every gateway cost derived from them —
was overstated by the thinking+markup share.

B7: completion tokens are counted on the cleaned reply plus the serialized
tool-call arguments the client actually receives.

Run:  python tests/test_usage_clean_text.py   (pytest-compatible)
"""
import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

MESSAGES = [{"role": "user", "content": "hello"}]
THINK_REPLY = "<think>" + "deep reasoning " * 200 + "</think>Answer."
TOOL_REPLY = (
    'Working on it.\n<tool_call name="Bash">'
    '<parameter name="command">ls -la</parameter></tool_call>'
)


def test_format_response_excludes_think_tokens():
    result = app_module.format_response(THINK_REPLY, "v4.1flash", MESSAGES)
    out = result["usage"]["completion_tokens"]
    raw = app_module.count_tok(THINK_REPLY)
    expected = app_module.count_tok("Answer.")
    assert out == expected, f"completion must be counted on the cleaned reply: {out} != {expected}"
    assert out < raw, "the think share must no longer be billed"


def test_format_response_counts_tool_arguments():
    result = app_module.format_response(TOOL_REPLY, "v4.1flash", MESSAGES)
    out = result["usage"]["completion_tokens"]
    cleaned = app_module._completion_usage_text("Working on it.", result["choices"][0]["message"]["tool_calls"])
    expected = app_module.count_tok(cleaned)
    assert out == expected, out
    assert "tool_calls" in result["choices"][0]["message"]


def test_completion_usage_text_shape():
    tools = [{"id": "call_1", "type": "function",
              "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]
    text = app_module._completion_usage_text("Doing it.", tools)
    assert "Doing it." in text and "Bash" in text and "ls" in text


async def _collect(agen):
    out = []
    async for line in agen:
        out.append(line)
    return out


def test_stream_usage_chunk_excludes_think_tokens():
    chunks = ["<think>", "reasoning ", "reasoning ", "</think>", "Visible ", "reply"]

    async def gen():
        for c in chunks:
            yield c

    lines = asyncio.run(_collect(app_module.stream_response(
        gen(), "v4.1flash", MESSAGES, 1, "sess", "sig", []
    )))
    payloads = []
    for line in lines:
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            payloads.append(json.loads(line[len("data: "):]))
    usage_chunks = [p for p in payloads if "usage" in p]
    assert len(usage_chunks) == 1, payloads
    expected = app_module.count_tok("Visible reply")
    got = usage_chunks[0]["usage"]["completion_tokens"]
    assert got == expected, f"stream usage must exclude think tokens: {got} != {expected}"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
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
