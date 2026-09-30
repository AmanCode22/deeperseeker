"""Disconnect-cancellation guarantee tests (B14, Stage 1 audit).

The claude-relay-service #1244 leak class: when a client disconnects
mid-stream, something must (a) close the upstream response so DeepSeek stops
generating into a dead connection (silently burning account budget), and
(b) purge the session rows so the next turn cannot fork the conversation.
The `async with resp` in send_message SHOULD have provided (a) — nothing
tested it, and nothing tested the B2 purge or the B3 slot release across the
teardown either. Now the guarantee is locked by tests:

  1. tearing down a send_message stream mid-flight closes the upstream
     response (aiohttp releases the connection in __aexit__);
  2. cancelling a mid-stream stream_response consumer purges the session
     rows (B2);
  3. _release_chat_lock_stream releases BOTH the transferred chat lock and
     the transferred token slot when the stream is torn down.

Run:  python tests/test_disconnect_cancellation.py   (pytest-compatible)
"""
import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402
import app as app_module  # noqa: E402


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def iter_any(self):
        async def _gen():
            for chunk in self._chunks:
                yield chunk

        return _gen()


class TrackingSSEResponse:
    """Minimal aiohttp-response stand-in that records __aexit__ — the point
    where a real aiohttp ClientResponse releases/closes the connection."""

    def __init__(self, body: bytes, status=200):
        self.status = status
        self._body = body
        self.closed = False
        self.content = _FakeStream([body[i : i + 17] for i in range(0, len(body), 17)])

    async def text(self):
        return self._body.decode("utf-8", errors="replace")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def _sse_body(lines=500):
    # No FINISHED event: the stream never completes, so teardown below happens
    # strictly mid-stream.
    return (
        'data: {"p":"response/fragments","o":"APPEND","v":[{"type":"RESPONSE","content":"mid"}]}\n\n' * lines
    ).encode("utf-8")


def test_tearing_down_send_message_closes_upstream_response():
    resp = TrackingSSEResponse(_sse_body())

    async def fake_pow(target_path, auth_token):
        return "pow"

    async def fake_post(*args, **kwargs):
        return resp

    async def run():
        orig_pow = functions.solve_create_pow
        orig_post = functions.post_with_failover
        functions.solve_create_pow = fake_pow
        functions.post_with_failover = fake_post
        try:
            gen = functions.send_message("chat-1", "tok", "hello", 0)
            it = gen.__aiter__()
            chunk = await it.__anext__()
            assert chunk, "expected the first fragment before teardown"
            await gen.aclose()  # client went away mid-stream
        finally:
            functions.solve_create_pow = orig_pow
            functions.post_with_failover = orig_post

    asyncio.run(run())
    assert resp.closed, (
        "tearing down a send_message stream must close the upstream response — "
        "otherwise upstream keeps generating into a dead connection"
    )


def test_cancelling_stream_consumer_purges_session_rows():
    deleted = []

    def fake_mark_active(tid):
        return None

    def fake_save(*a, **k):
        return None

    def fake_delete(tid, sid):
        deleted.append((tid, sid))

    async def scenario():
        app_module._chat_locks.clear()
        names = ("mark_active", "save_session", "delete_sessions_for_chat")
        saved = [(n, getattr(app_module, n)) for n in names]
        app_module.mark_active = fake_mark_active
        app_module.save_session = fake_save
        app_module.delete_sessions_for_chat = fake_delete
        try:
            async def slow_gen():
                for i in range(200):
                    yield f"piece {i} "
                    await asyncio.sleep(0.001)

            gen = app_module.stream_response(
                slow_gen(), "v4.1flash", [{"role": "user", "content": "hi"}], 1, "sess-cancel", "sig", []
            )
            received = 0
            async for _chunk in gen:
                received += 1
                if received >= 2:
                    break  # the client stops reading mid-stream...
            await gen.aclose()  # ...and the transport tears the request down

            assert not app_module._chat_lock("sess-cancel").locked()
        finally:
            for n, fn in saved:
                setattr(app_module, n, fn)

    asyncio.run(scenario())
    assert (1, "sess-cancel") in deleted, deleted


def test_stream_wrapper_releases_transferred_lock_and_slot():
    async def run():
        app_module._chat_locks.clear()
        lock = app_module._chat_lock("sess-wrap")
        owner = app_module._OwnedChatLock(lock)
        await owner.acquire()
        slot = functions.acquire_token_slot(1)
        assert functions.token_in_flight(1) == 1

        async def gen():
            yield "a "
            yield "b "
            yield "c"

        wrapped = app_module._release_chat_lock_stream(gen(), owner, slot)
        out = []
        async for chunk in wrapped:
            out.append(chunk)
            if len(out) == 2:
                break  # abort mid-stream
        await wrapped.aclose()

        assert not owner.owned, "the transferred lock must be released on teardown"
        assert not lock.locked()
        assert functions.token_in_flight(1) == 0, "the transferred slot must be released on teardown"

    asyncio.run(run())


TESTS = [
    test_tearing_down_send_message_closes_upstream_response,
    test_cancelling_stream_consumer_purges_session_rows,
    test_stream_wrapper_releases_transferred_lock_and_slot,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
