"""Regression tests for DeepSeek upstream SSE parsing (issue #33)."""

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


class FakeSSEResponse:
    def __init__(self, body: bytes, status=200):
        self.status = status
        self._body = body
        self.content = _FakeStream(self._chunk_body(body))

    @staticmethod
    def _chunk_body(body: bytes, piece=17):
        return [body[i : i + piece] for i in range(0, len(body), piece)] if body else []

    async def text(self):
        return self._body.decode("utf-8", errors="replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _collect_send_message(body: bytes):
    import functions

    async def fake_pow(target_path, auth_token):
        return "pow"

    async def fake_post(*args, **kwargs):
        return FakeSSEResponse(body)

    orig_pow = functions.solve_create_pow
    orig_post = functions.post_with_failover
    functions.solve_create_pow = fake_pow
    functions.post_with_failover = fake_post
    try:
        parts = []
        async for chunk in functions.send_message("chat-1", "tok", "hello", 0):
            parts.append(chunk)
        return "".join(parts)
    finally:
        functions.solve_create_pow = orig_pow
        functions.post_with_failover = orig_post


async def _expect_send_error(body: bytes):
    import functions

    async def fake_pow(target_path, auth_token):
        return "pow"

    async def fake_post(*args, **kwargs):
        return FakeSSEResponse(body)

    orig_pow = functions.solve_create_pow
    orig_post = functions.post_with_failover
    functions.solve_create_pow = fake_pow
    functions.post_with_failover = fake_post
    try:
        async for _ in functions.send_message("chat-1", "tok", "hello", 0):
            pass
        raise AssertionError("expected send_message to fail")
    except Exception as e:
        return e
    finally:
        functions.solve_create_pow = orig_pow
        functions.post_with_failover = orig_post


def test_sse_line_split_across_tcp_chunks():
    lines = (
        'data: {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"chunked"}]}\n\n'
        'data: {"p":"response/status","v":"FINISHED"}\n'
    )
    body = lines.encode("utf-8")
    mid = len(body) // 2
    chunks = [body[:mid], body[mid:]]

    import functions

    async def fake_pow(target_path, auth_token):
        return "pow"

    async def fake_post(*args, **kwargs):
        resp = FakeSSEResponse(b"")
        resp.content = _FakeStream(chunks)
        return resp

    async def run():
        orig_pow = functions.solve_create_pow
        orig_post = functions.post_with_failover
        functions.solve_create_pow = fake_pow
        functions.post_with_failover = fake_post
        try:
            parts = []
            async for chunk in functions.send_message("chat-1", "tok", "hello", 0):
                parts.append(chunk)
            return "".join(parts)
        finally:
            functions.solve_create_pow = orig_pow
            functions.post_with_failover = orig_post

    assert asyncio.run(run()) == "chunked"


def test_batch_append_after_quasi_finished():
    import json

    batch = {
        "o": "BATCH",
        "v": [
            {"p": "quasi_status", "v": "FINISHED"},
            {
                "p": "response/fragments",
                "o": "APPEND",
                "v": [{"type": "RESPONSE", "content": "from-batch"}],
            },
        ],
    }
    body = (
        f"data: {json.dumps(batch, separators=(',', ':'))}\n"
        'data: {"p":"response/status","v":"FINISHED"}\n'
    ).encode()
    assert asyncio.run(_collect_send_message(body)) == "from-batch"


def test_rate_limit_error_raises_http_429():
    body = (
        b'data: {"type":"error","content":"Messages too frequent. Try again later.",'
        b'"finish_reason":"rate_limit_reached"}\n'
    )
    err = asyncio.run(_expect_send_error(body))
    assert "HTTP 429:" in str(err)
    assert "too frequent" in str(err).lower()


def test_finished_without_output_default_message():
    body = b'data: {"p":"response/status","v":"FINISHED"}\n'
    err = asyncio.run(_expect_send_error(body))
    assert "no parseable SSE content" in str(err)


def test_handle_chat_retries_empty_sse_on_parent_zero():
    import app as app_module

    calls = {"send": 0}

    async def scenario():
        app_module._chat_locks.clear()
        sig = "sig-empty-parent0"
        session = {
            "token_id": "t1",
            "session_id": "chat-empty-1",
            "parent_message_id": 0,
        }

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_find(s):
            return dict(session)

        def fake_get_token(tid):
            return {"token": "tok", "status": "ACTIVE"}

        def fake_send(
            chat_id,
            auth_token,
            message,
            parent,
            thinking=False,
            search=False,
            file_ids_=None,
        ):
            calls["send"] += 1
            if calls["send"] == 1:

                async def fail_gen():
                    if False:
                        yield ""
                    raise Exception(
                        "Empty response from DeepSeek (no parseable SSE content): stream ended"
                    )

                return fail_gen()

            async def gen():
                yield "recovered"

            return gen()

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", fake_find),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "test-model"
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    result = asyncio.run(scenario())
    assert result == "recovered"
    assert calls["send"] == 2


def test_handle_chat_rotates_to_another_token_on_429():
    import app as app_module

    calls = {"send": 0, "limited": []}
    session = {"token_id": 1, "session_id": "chat-429", "parent_message_id": 20}
    sig = "sig-429-rotate"

    async def fake_sig(messages, model, scope=""):
        return sig

    def fake_find(s):
        if calls["send"] == 0:
            return dict(session)
        return None

    def fake_pick(*a, **k):
        return 2 if calls["send"] >= 1 else 1

    def fake_get_token(tid):
        return {"token": f"tok-{tid}", "status": "ACTIVE", "alias": f"t{tid}"}

    def fake_send(
        chat_id,
        auth_token,
        message,
        parent,
        thinking=False,
        search=False,
        file_ids_=None,
    ):
        calls["send"] += 1
        if calls["send"] == 1:

            async def fail_gen():
                # B6: typed upstream status — the old string protocol is gone
                raise functions.UpstreamError(
                    429, "Messages too frequent. Try again later."
                )
                yield ""  # pragma: no cover

            return fail_gen()

        async def ok_gen():
            yield "ok after rotate"

        return ok_gen()

    async def fake_create_chat(token):
        return f"new-chat-{token}"

    async def fake_files(messages, token, last_user_only=False):
        return []

    async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
        return "prompt"

    def fake_limited(tid):
        calls["limited"].append(tid)

    patches = [
        ("get_auth_token", lambda: "tok"),
        ("generate_signature", fake_sig),
        ("find_session", fake_find),
        ("pick_token", fake_pick),
        ("get_token", fake_get_token),
        ("send_message", fake_send),
        ("create_new_chat", fake_create_chat),
        ("extract_and_upload_files", fake_files),
        ("build_prompt", fake_prompt),
        ("mark_limited", fake_limited),
        ("mark_active", lambda tid: None),
        ("delete_sessions_for_chat", lambda *a: None),
        ("save_session", lambda *a: None),
        ("parse_tools", lambda t: ([], t)),
        ("format_response", lambda text, model, messages, tools=None: text),
    ]

    async def run():
        app_module._chat_locks.clear()
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "test-model"
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    assert asyncio.run(run()) == "ok after rotate"
    assert calls["send"] == 2
    assert calls["limited"] == [1]


TESTS = [
    test_sse_line_split_across_tcp_chunks,
    test_batch_append_after_quasi_finished,
    test_rate_limit_error_raises_http_429,
    test_finished_without_output_default_message,
    test_handle_chat_retries_empty_sse_on_parent_zero,
    test_handle_chat_rotates_to_another_token_on_429,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
