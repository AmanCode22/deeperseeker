"""Stage 0 core-rails tests (middleware, TRUSTED_PROXIES, per-chat locks,
dual-endpoint failover).

pytest-compatible; also runnable directly:
    python tests/test_stage0_rails.py
"""
import asyncio
import json
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp

import middleware
from middleware import (
    RecovererMiddleware,
    RealIPMiddleware,
    RequestIDMiddleware,
    get_real_ip,
    parse_trusted_proxies,
)


# ------------------------------------------------------------------------------
# Helpers

def make_scope(client_ip="203.0.113.7", xff=None, request_id=None, path="/v1/chat/completions", method="POST"):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode("latin-1")))
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "client": (client_ip, 54321),
        "headers": headers,
    }


async def run_asgi(app, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    return sent


def ok_response(app_or_none=None):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}"})

    return inner


class _RestoreTrusted:
    """Context manager to swap middleware.TRUSTED_PROXIES and restore it."""

    def __init__(self, value):
        self.value = value
        self.orig = None

    def __enter__(self):
        self.orig = middleware.TRUSTED_PROXIES
        middleware.TRUSTED_PROXIES = self.value
        return self

    def __exit__(self, *exc):
        middleware.TRUSTED_PROXIES = self.orig


# ------------------------------------------------------------------------------
# Stage 0.2 — TRUSTED_PROXIES parsing + fail-closed XFF resolution

def test_parse_trusted_proxies_valid_and_invalid():
    nets = parse_trusted_proxies("10.0.0.0/8, 172.16.0.0/12, 127.0.0.1/32")
    assert len(nets) == 3
    assert parse_trusted_proxies("") == []
    assert parse_trusted_proxies(None) == []
    # invalid entries are skipped (fail closed), valid ones kept
    nets = parse_trusted_proxies("10.0.0.0/8, not-a-cidr, 192.168.1.0/24")
    assert len(nets) == 2


def test_real_ip_fail_closed_without_trusted_proxies():
    # No allowlist configured: XFF must be ignored entirely, even though the
    # client claims to be behind a proxy — spoofable headers are worthless.
    with _RestoreTrusted([]):
        scope = make_scope(client_ip="198.51.100.9", xff="1.2.3.4")
        assert get_real_ip(scope) == "198.51.100.9"


def test_real_ip_spoofed_xff_from_untrusted_peer_is_ignored():
    with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
        # Peer is NOT in the trusted range -> its XFF header counts for nothing
        scope = make_scope(client_ip="198.51.100.9", xff="1.2.3.4")
        assert get_real_ip(scope) == "198.51.100.9"


def test_real_ip_single_trusted_proxy():
    with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
        scope = make_scope(client_ip="10.0.0.2", xff="203.0.113.50")
        assert get_real_ip(scope) == "203.0.113.50"


def test_real_ip_walk_stops_at_untrusted_reporter():
    with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
        # Chain: attacker appended a fake hop AFTER the proxy appended the real
        # client. The real client is untrusted -> walk must stop there, never
        # adopt the attacker's forged leftmost entry.
        scope = make_scope(client_ip="10.0.0.2", xff="6.6.6.6, 203.0.113.50")
        assert get_real_ip(scope) == "203.0.113.50"


def test_real_ip_garbage_hop_stops_walk_fail_closed():
    with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
        # Malformed hop: abort the walk rather than skipping over it.
        scope = make_scope(client_ip="10.0.0.2", xff="not-an-ip, 203.0.113.50")
        assert get_real_ip(scope) == "203.0.113.50"

        # All-proxy chain (every reporter trusted) resolves to the origin
        scope = make_scope(client_ip="10.0.0.3", xff="10.0.0.2, 203.0.113.77")
        assert get_real_ip(scope) == "203.0.113.77"


def test_real_ip_trusted_peer_without_xff():
    with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
        scope = make_scope(client_ip="10.0.0.2")
        assert get_real_ip(scope) == "10.0.0.2"


# ------------------------------------------------------------------------------
# Stage 0.1 — RealIP / RequestID / Recoverer middlewares (pure ASGI)

def test_realip_middleware_sets_state():
    async def scenario():
        with _RestoreTrusted(parse_trusted_proxies("10.0.0.0/8")):
            captured = {}

            async def inner(scope, receive, send):
                captured["ip"] = scope["state"]["real_ip"]

            await RealIPMiddleware(inner)(make_scope("10.0.0.2", "203.0.113.9"), None, None)
            return captured["ip"]

    assert asyncio.run(scenario()) == "203.0.113.9"


def test_requestid_middleware_generates_and_echoes_header():
    async def scenario():
        sent = await run_asgi(RequestIDMiddleware(ok_response()), make_scope())
        start = sent[0]
        header = [v for k, v in start["headers"] if k == b"x-request-id"]
        return header[0].decode()

    rid = asyncio.run(scenario())
    assert len(rid) == 32  # uuid4().hex


def test_requestid_middleware_honors_sane_inbound_id():
    async def scenario():
        sent = await run_asgi(RequestIDMiddleware(ok_response()), make_scope(request_id="trace-abc.123"))
        return [v for k, v in sent[0]["headers"] if k == b"x-request-id"][0].decode()

    assert asyncio.run(scenario()) == "trace-abc.123"


def test_requestid_middleware_rejects_malicious_inbound_id():
    async def scenario():
        evil = "bad\r\nX-Injected: 1"
        sent = await run_asgi(RequestIDMiddleware(ok_response()), make_scope(request_id=evil))
        rid = [v for k, v in sent[0]["headers"] if k == b"x-request-id"][0].decode()
        return rid

    rid = asyncio.run(scenario())
    assert rid != "bad\r\nX-Injected: 1"
    assert all(ch.isalnum() or ch in ".-_" for ch in rid)


def test_recoverer_turns_crash_into_json_500_with_request_id():
    async def crashing(scope, receive, send):
        raise RuntimeError("boom")

    async def scenario():
        scope = make_scope()
        scope["state"] = {"request_id": "req-42"}
        sent = await run_asgi(RecovererMiddleware(crashing), scope)
        return sent

    sent = asyncio.run(scenario())
    assert sent[0]["status"] == 500
    body = json.loads(sent[1]["body"].decode())
    assert body["error"]["request_id"] == "req-42"
    assert body["error"]["type"] == "internal_error"
    hdr = [v for k, v in sent[0]["headers"] if k == b"x-request-id"][0].decode()
    assert hdr == "req-42"


def test_recoverer_reraises_when_response_already_started():
    async def midstream_crash(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("mid-stream failure")

    async def scenario():
        sent = []

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            sent.append(message)

        await RecovererMiddleware(midstream_crash)(make_scope(), receive, send)

    raised = False
    try:
        asyncio.run(scenario())
    except RuntimeError:
        raised = True
    assert raised, "mid-stream crash must propagate (cannot send a second response)"


def test_recoverer_passthrough_on_success():
    async def scenario():
        sent = await run_asgi(RecovererMiddleware(ok_response()), make_scope())
        return sent[0]["status"]

    assert asyncio.run(scenario()) == 200


# ------------------------------------------------------------------------------
# Stage 0.3 — Per-chat locks

def test_chat_lock_same_key_same_lock_different_key_different_lock():
    import app as app_module

    a1 = app_module._chat_lock("chat-a")
    a2 = app_module._chat_lock("chat-a")
    b1 = app_module._chat_lock("chat-b")
    assert a1 is a2
    assert a1 is not b1


def test_chat_lock_serializes_same_chat_requests():
    import app as app_module

    async def scenario():
        lock = app_module._chat_lock("chat-serial")
        await lock.acquire()
        progress = []

        async def worker():
            async with lock:
                progress.append("entered")

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.05)
        assert progress == [], "second same-chat request must wait while the lock is held"
        lock.release()
        await task
        assert progress == ["entered"]

    asyncio.run(scenario())


def test_chat_lock_eviction_respects_held_locks():
    import app as app_module

    async def scenario():
        held = app_module._chat_lock("held")  # stays locked
        await held.acquire()
        app_module._chat_lock("free-1")
        app_module._chat_lock("free-2")  # cap reached -> eviction of unlocked keys
        lock = app_module._chat_lock("new-chat")
        assert lock is not None
        assert "held" in app_module._chat_locks, "locked entry must never be evicted"
        assert "new-chat" in app_module._chat_locks
        held.release()

    original = dict(app_module._chat_locks)
    original_cap = app_module.CHAT_LOCKS_MAX
    try:
        app_module._chat_locks.clear()
        app_module.CHAT_LOCKS_MAX = 2
        asyncio.run(scenario())
    finally:
        app_module._chat_locks.clear()
        app_module._chat_locks.update(original)
        app_module.CHAT_LOCKS_MAX = original_cap


def test_release_chat_lock_stream_releases_on_completion():
    import app as app_module

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()

        async def gen():
            yield "a"
            yield "b"

        wrapped = app_module._release_chat_lock_stream(gen(), lock)
        out = [chunk async for chunk in wrapped]
        return out, lock

    out, lock = asyncio.run(scenario())
    assert out == ["a", "b"]
    assert not lock.locked(), "lock must be released after the stream completes"


def test_release_chat_lock_stream_releases_on_client_abort():
    import app as app_module

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()

        async def gen():
            yield "a"
            yield "b"  # never consumed

        wrapped = app_module._release_chat_lock_stream(gen(), lock)
        it = wrapped.__aiter__()
        await it.__anext__()
        await it.aclose()  # simulates client abort mid-stream (GeneratorExit)
        return lock

    lock = asyncio.run(scenario())
    assert not lock.locked(), "lock must be released when the client aborts mid-stream"


# ------------------------------------------------------------------------------
# Stage 0.4 — Dual-endpoint failover

class FakeResp:
    def __init__(self, status, text="err"):
        self.status = status
        self._text = text

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    """Records POST urls; replays scripted outcomes (FakeResp or Exception)."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.urls = []

    async def post(self, url, **kwargs):
        self.urls.append(url)
        item = self.outcomes.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


async def call_failover(session_stub, bases):
    import functions

    orig = functions.UPSTREAM_BASES
    functions.UPSTREAM_BASES = list(bases)
    try:
        return await functions.post_with_failover("/api/v0/x", headers={}, session=session_stub)
    finally:
        functions.UPSTREAM_BASES = orig


def test_failover_replays_on_5xx():
    s = FakeSession([FakeResp(500, "boom"), FakeResp(200, "{}")])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 200
    assert s.urls == ["http://primary/api/v0/x", "http://backup/api/v0/x"]


def test_failover_replays_on_connection_error():
    s = FakeSession([aiohttp.ClientConnectionError("refused"), FakeResp(200, "{}")])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 200


def test_failover_last_5xx_returned_as_is():
    # Both endpoints 5xx: the final response is returned unchanged so callers
    # keep their existing error handling (send_message raises its clean error).
    s = FakeSession([FakeResp(500, "a"), FakeResp(503, "b")])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 503
    assert len(s.urls) == 2


def test_failover_last_connection_error_raises():
    s = FakeSession([
        aiohttp.ClientConnectionError("down-1"),
        aiohttp.ClientConnectionError("down-2"),
    ])
    raised = False
    try:
        asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    except aiohttp.ClientConnectionError:
        raised = True
    assert raised


def test_failover_4xx_never_replays():
    # A bad token is bad on every endpoint: fail fast, single call.
    s = FakeSession([FakeResp(401, "unauthorized"), FakeResp(200, "{}")])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 401
    assert len(s.urls) == 1


def test_failover_single_endpoint_mode_unchanged():
    # No fallback configured: behaves exactly like the old direct POST.
    s = FakeSession([FakeResp(502, "bad gateway")])
    resp = asyncio.run(call_failover(s, ["http://primary"]))
    assert resp.status == 502
    assert len(s.urls) == 1


def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback

            print(f"FAIL {fn.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
