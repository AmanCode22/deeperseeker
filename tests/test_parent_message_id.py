"""DeepSeek 2026-10-31 protocol change: the first message must not carry
`parent_message_id`.

Upstream started rejecting `parent_message_id: 0` with biz_code 26
"invalid message id", delivered as `application/json` on an endpoint the
client parses as SSE -- so the failure surfaced as a silent empty stream.
Different edges also disagree on whether `null` is tolerated, so the only
shape that works everywhere is omitting the key on the first turn.
Follow-up turns keep the existing parent = previous response id chain.

See PR #92 and the incident report on it.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def iter_any(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class _FakeResponse:
    def __init__(self, body: bytes, status=200, content_type="text/event-stream"):
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body
        self.content = _FakeStream([body] if body else [])

    async def text(self):
        return self._body.decode("utf-8", errors="replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _run_send(body: bytes, parent, content_type="text/event-stream"):
    """Drive send_message with a fake upstream; return (payloads, output|error)."""

    captured = []

    async def fake_pow(target_path, auth_token):
        return "pow"

    async def fake_post(path, headers=None, json=None, **kwargs):
        captured.append(dict(json or {}))
        return _FakeResponse(body, content_type=content_type)

    async def run():
        orig_pow = functions.solve_create_pow
        orig_post = functions.post_with_failover
        functions.solve_create_pow = fake_pow
        functions.post_with_failover = fake_post
        try:
            parts = []
            async for chunk in functions.send_message("chat-1", "tok", "hello", parent):
                parts.append(chunk)
            return "".join(parts)
        finally:
            functions.solve_create_pow = orig_pow
            functions.post_with_failover = orig_post

    try:
        return captured, asyncio.run(run())
    except Exception as e:  # noqa: BLE001 - the test inspects the error
        return captured, e


_OK_BODY = (
    b'data: {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"hi"}]}\n'
    b'data: {"p":"response/status","v":"FINISHED"}\n'
)


def test_first_message_omits_parent_message_id_key():
    """parent=0 is the first turn; the key must be absent, not 0 and not null."""
    payloads, out = _run_send(_OK_BODY, 0)
    assert out == "hi"
    assert "parent_message_id" not in payloads[0]


def test_followup_message_keeps_parent_message_id():
    """Later turns still chain off the previous response id."""
    payloads, out = _run_send(_OK_BODY, 12)
    assert out == "hi"
    assert payloads[0]["parent_message_id"] == 12


def test_non_sse_json_error_is_reported_not_silent():
    """A JSON biz error on the SSE endpoint names its code and message."""
    body = b'{"code":26,"msg":"invalid message id","data":null}'
    payloads, err = _run_send(body, 0, content_type="application/json")

    assert isinstance(err, functions.UpstreamError), err
    text = str(err)
    assert "no parseable SSE content" in text
    assert "code=26" in text
    assert "invalid message id" in text
    # The body never reached the parser as content.
    assert "data:" not in text


def test_non_sse_error_does_not_rotate_tokens():
    """A protocol error is not a token error: keep the 502, do not cool the pool."""
    body = b'{"code":26,"msg":"invalid message id","data":null}'
    _, err = _run_send(body, 0, content_type="application/json")
    assert isinstance(err, functions.UpstreamError)
    assert err.status == 502


TESTS = [
    test_first_message_omits_parent_message_id_key,
    test_followup_message_keeps_parent_message_id,
    test_non_sse_json_error_is_reported_not_silent,
    test_non_sse_error_does_not_rotate_tokens,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()