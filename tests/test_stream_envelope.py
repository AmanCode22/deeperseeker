"""Regression tests for the shared OpenAI stream chunk envelope (Stage 2).

_chat_chunk() already gives every frame the spec-shaped envelope strict
clients require (Vercel AI SDK, Zed), but the emission sites did not pass
cid/created — so every chunk of one completion minted a fresh id/created.
Clients and gateways (New API, sub2api) group and bill a stream by its id;
one completion must therefore be exactly one id + one created, shared by
every reasoning / content / tool_calls / finish / usage chunk of the stream.
The Anthropic path already hoists a per-completion msg_id — this locks the
same guarantee on the OpenAI path. Error payloads are deliberately NOT
enveloped — an error is not a completion chunk.

Run:  python tests/test_stream_envelope.py   (pytest-compatible)
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402

MESSAGES = [{"role": "user", "content": "hello"}]


async def _collect(agen):
    out = []
    async for line in agen:
        out.append(line)
    return out


def _payloads(chunks):
    async def gen():
        for c in chunks:
            yield c

    lines = asyncio.run(_collect(app_module.stream_response(
        gen(), "v4.1flash", MESSAGES, 1, "sess", "sig", []
    )))
    out = []
    for line in lines:
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            out.append(json.loads(line[len("data: "):]))
    return out


def test_stream_chunks_share_one_id_and_created():
    payloads = _payloads(["Hello ", "world"])
    assert payloads, "stream must emit chunks"
    ids = {p["id"] for p in payloads}
    created = {p["created"] for p in payloads}
    assert len(ids) == 1, f"every chunk of a completion must share one id: {ids}"
    assert len(created) == 1, f"every chunk of a completion must share one created: {created}"
    assert ids.pop().startswith("chatcmpl-")
    assert all(p["object"] == "chat.completion.chunk" for p in payloads), payloads
    # the finish chunk still terminates the choices array properly
    finish = [p for p in payloads if p["choices"] and p["choices"][0]["finish_reason"] == "stop"]
    assert len(finish) == 1, payloads


def test_think_and_content_chunks_share_the_envelope():
    payloads = _payloads(["<think>", "reasoning ", "</think>", "Visible ", "reply"])
    ids = {p["id"] for p in payloads}
    assert len(ids) == 1, ids
    reasoning = [p for p in payloads if p["choices"] and "reasoning_content" in p["choices"][0]["delta"]]
    content = [p for p in payloads if p["choices"] and "content" in p["choices"][0]["delta"]]
    assert reasoning and content, payloads
    assert reasoning[0]["id"] == content[0]["id"]


def test_usage_chunk_carries_the_same_envelope():
    payloads = _payloads(["Visible reply"])
    usage = [p for p in payloads if "usage" in p]
    assert len(usage) == 1, payloads
    assert usage[0]["choices"] == [], "usage chunk must keep the empty choices array"
    first = [p for p in payloads if p["choices"]][0]
    assert usage[0]["id"] == first["id"], "gateways match the usage chunk to the stream by id"
    assert usage[0]["created"] == first["created"]


def test_error_payload_is_not_enveloped():
    async def gen():
        yield "partial "
        raise RuntimeError("upstream boom")

    lines = asyncio.run(_collect(app_module.stream_response(
        gen(), "v4.1flash", MESSAGES, 1, "sess", "sig", []
    )))
    errors = []
    for line in lines:
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            p = json.loads(line[len("data: "):])
            if "error" in p:
                errors.append(p)
    assert len(errors) == 1, lines
    assert "id" not in errors[0], "an error payload is not a completion chunk"


TESTS = [
    test_stream_chunks_share_one_id_and_created,
    test_think_and_content_chunks_share_the_envelope,
    test_usage_chunk_carries_the_same_envelope,
    test_error_payload_is_not_enveloped,
]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    sys.exit(1 if failed else 0)
