"""Stage 0 core-rails tests (middleware, TRUSTED_PROXIES, per-chat locks,
dual-endpoint failover).

pytest-compatible; also runnable directly:
    python tests/test_stage0_rails.py
"""
import asyncio
import contextlib
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
        token = app_module._OwnedChatLock(lock)
        await token.acquire()

        async def gen():
            yield "a"
            yield "b"

        wrapped = app_module._release_chat_lock_stream(gen(), token)
        out = [chunk async for chunk in wrapped]
        return out, lock

    out, lock = asyncio.run(scenario())
    assert out == ["a", "b"]
    assert not lock.locked(), "lock must be released after the stream completes"


def test_release_chat_lock_stream_releases_on_client_abort():
    import app as app_module

    async def scenario():
        lock = asyncio.Lock()
        token = app_module._OwnedChatLock(lock)
        await token.acquire()

        async def gen():
            yield "a"
            yield "b"  # never consumed

        wrapped = app_module._release_chat_lock_stream(gen(), token)
        it = wrapped.__aiter__()
        await it.__anext__()
        await it.aclose()  # simulates client abort mid-stream (GeneratorExit)
        return lock

    lock = asyncio.run(scenario())
    assert not lock.locked(), "lock must be released when the client aborts mid-stream"


def test_release_chat_lock_stream_cannot_release_strangers_acquisition():
    # PR #26 review (Medium): the old `if lock.locked(): lock.release()`
    # teardown dropped whoever held the lock. A token must only ever release
    # ITS OWN acquisition — even when it never owned the lock at all.
    import app as app_module

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()  # "stranger" (another request) holds the lock

        async def gen():
            yield "a"

        token = app_module._OwnedChatLock(lock)  # not owned by this holder
        wrapped = app_module._release_chat_lock_stream(gen(), token)
        out = [chunk async for chunk in wrapped]  # teardown calls token.release()
        return out, lock.locked()

    out, still_locked = asyncio.run(scenario())
    assert out == ["a"]
    assert still_locked, "stream teardown must not drop another holder's acquisition"


def test_owned_chat_lock_release_is_idempotent_and_scoped():
    import app as app_module

    async def scenario():
        lock = asyncio.Lock()
        owner = app_module._OwnedChatLock(lock)
        assert not owner.owned
        owner.release()  # releasing before acquiring is a harmless no-op
        await owner.acquire()
        assert owner.owned and lock.locked()
        owner.release()
        assert not lock.locked()
        owner.release()  # idempotent — no RuntimeError, no double release
        # a stale token must not release someone else's later acquisition
        await lock.acquire()
        owner.release()
        return lock.locked()

    assert asyncio.run(scenario()) is True


# ------------------------------------------------------------------------------
# PR #26 review (High), plus post-review addendum — the lock registry must
# preserve same-chat identity under pressure AND stay bounded.

def test_chat_lock_over_cap_preserves_same_chat_identity_across_pressure_drop():
    """The addendum's core scenario. With the registry full and every entry
    held, chat-x registers over cap. If pressure then DROPS while that
    request is still in flight, the next request for chat-x must resolve to
    the SAME lock object and wait — a fresh per-key lock beside the still-held
    one would put two live holders in one chat's critical section (the
    parent_message_id race Stage 0.3 exists to close). The earlier
    shared-fallback draft failed exactly here: it never registered the key,
    so the post-drop request created a new lock."""
    import app as app_module

    async def scenario():
        held = [app_module._chat_lock(f"held-{i}") for i in range(2)]
        for lk in held:
            await lk.acquire()
        a = app_module._chat_lock("chat-x")  # registered over cap (everything held)
        await a.acquire()
        held[1].release()  # pressure drops while chat-x's request is still in flight
        b = app_module._chat_lock("chat-x")
        assert b is a, "same chat must resolve to the same lock object across a pressure drop"
        entered = []

        async def second_holder():
            async with b:
                entered.append("in")

        task = asyncio.create_task(second_holder())
        await asyncio.sleep(0.05)
        assert entered == [], "no second concurrent holder for the same chat, even over cap"
        a.release()
        await task
        assert entered == ["in"]
        held[0].release()  # held-1 was already released to simulate the pressure drop

    _with_chat_lock_registry(2, scenario)


def test_chat_lock_over_cap_depth_is_bounded_and_stable():
    """Over-cap depth tracks in-flight pressure, not chat history: entries
    are added only while every existing lock is held; distinct unheld chats
    recycle unlocked slots instead of growing the registry; and drained
    over-cap entries stay inert (capped at the pressure peak) until churned
    out. The 23-entries-with-cap-4 leak shape must not return."""
    import app as app_module

    async def scenario():
        held = [app_module._chat_lock(f"held-{i}") for i in range(2)]
        for lk in held:
            await lk.acquire()
        burst = []
        for i in range(4):
            lk = app_module._chat_lock(f"burst-{i}")  # over cap: everything held
            await lk.acquire()
            burst.append(lk)
        assert len(app_module._chat_locks) == 6, "depth == registry at cap + the four in-flight registrations"
        held[1].release()  # one recycle slot opens
        for i in range(10):
            app_module._chat_lock(f"churn-{i}")  # distinct unheld chats
        assert len(app_module._chat_locks) == 6, "churn must recycle slots, not grow the registry"
        for lk in burst:
            lk.release()
        held[0].release()  # held-1 was already released to open the recycle slot

    _with_chat_lock_registry(2, scenario)


def test_chat_lock_over_cap_registration_logs_warning_with_depth():
    import app as app_module

    async def scenario():
        records = []

        class _Rec:
            def warning(self, msg, *args):
                records.append(msg % args if args else msg)

        orig_logger = app_module.logger
        app_module.logger = _Rec()
        try:
            held = [app_module._chat_lock(f"held-{i}") for i in range(2)]
            for lk in held:
                await lk.acquire()
            app_module._chat_lock("chat-x")  # over cap -> one warning with depth
            app_module._chat_lock("held-0")  # hit: no log, no growth
            for lk in held:
                lk.release()
        finally:
            app_module.logger = orig_logger
        assert len(records) == 1, "only the over-cap registration logs"
        assert "over cap: 3 entries (cap 2)" in records[0]

    _with_chat_lock_registry(2, scenario)


def test_sig_lock_registry_over_cap_registration_keeps_identity():
    import app as app_module

    async def scenario():
        held = [
            app_module._take_lock("sig", app_module._sig_locks, f"sig-{i}", 2)
            for i in range(2)
        ]
        for lk in held:
            await lk.acquire()
        lock = app_module._take_lock("sig", app_module._sig_locks, "sig-new", 2)  # over cap
        again = app_module._take_lock("sig", app_module._sig_locks, "sig-new", 2)  # hit
        assert lock is again, "same signature must resolve to the same lock object over cap"
        assert len(app_module._sig_locks) == 3
        for lk in held:
            lk.release()

    orig = list(app_module._sig_locks.items())
    try:
        app_module._sig_locks.clear()
        asyncio.run(scenario())
    finally:
        app_module._sig_locks.clear()
        app_module._sig_locks.update(orig)


def _with_chat_lock_registry(cap, scenario):
    """Run a scenario against an empty chat-lock registry of the given cap."""
    import app as app_module

    orig = list(app_module._chat_locks.items())
    orig_cap = app_module.CHAT_LOCKS_MAX
    try:
        app_module._chat_locks.clear()
        app_module.CHAT_LOCKS_MAX = cap
        asyncio.run(scenario())
    finally:
        app_module._chat_locks.clear()
        app_module._chat_locks.update(orig)
        app_module.CHAT_LOCKS_MAX = orig_cap


def test_chat_lock_eviction_evicts_oldest_unlocked_and_keeps_cap():
    import app as app_module

    async def scenario():
        old = app_module._chat_lock("old")
        await old.acquire()
        app_module._chat_lock("mid")
        app_module._chat_lock("newest")  # cap reached
        app_module._chat_lock("extra")  # over cap -> LRU unlocked ("mid") evicted
        assert "mid" not in app_module._chat_locks
        assert "old" in app_module._chat_locks, "locked entry is never evicted"
        assert "newest" in app_module._chat_locks
        assert "extra" in app_module._chat_locks
        assert len(app_module._chat_locks) == 3
        old.release()

    _with_chat_lock_registry(3, scenario)


def test_chat_lock_touch_refreshes_lru_position():
    import app as app_module

    async def scenario():
        app_module._chat_lock("a")
        app_module._chat_lock("b")
        app_module._chat_lock("c")  # cap reached
        app_module._chat_lock("a")  # touch -> "a" becomes most-recently-used
        app_module._chat_lock("d")  # over cap -> must evict "b", not "a"
        assert "b" not in app_module._chat_locks
        assert "a" in app_module._chat_locks

    _with_chat_lock_registry(3, scenario)


# ------------------------------------------------------------------------------
# Stage 0.4 — Dual-endpoint failover

class FakeResp:
    def __init__(self, status, text="err", json_data=None):
        self.status = status
        self._text = text
        self._json = json_data
        self.released = False

    async def text(self):
        return self._text

    async def json(self):
        if self._json is None:
            raise ValueError("no json body scripted for this FakeResp")
        return self._json

    def release(self):
        self.released = True

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
    first = FakeResp(500, "a")
    last = FakeResp(503, "b")
    s = FakeSession([first, last])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 503
    assert len(s.urls) == 2
    assert first.released, "the failed non-last 5xx response must be released"
    assert not last.released, "the returned response stays owned by the caller"


def test_failover_releases_failed_5xx_response_before_failing_over():
    # PR #26 review (Medium): the 5xx failover path read the body and moved on
    # without releasing — repeated failovers leaked pool connections.
    bad = FakeResp(500, "boom")
    good = FakeResp(200, "{}")
    s = FakeSession([bad, good])
    resp = asyncio.run(call_failover(s, ["http://primary", "http://backup"]))
    assert resp.status == 200
    assert bad.released, "failed 5xx response must be handed back to the connection pool"
    assert not good.released, "the success response remains owned by the caller"


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


# ------------------------------------------------------------------------------
# PR #26 review (Blocker 1) — call-site smoke tests
#
# post_with_failover is a coroutine returning an owned response. Three of the
# four call sites still used `async with post_with_failover(...)`, which dies
# with `AttributeError: __aenter__` — taking down create_new_chat (every new
# conversation) and create_challange_pow (the PoW header gate). The old suite
# was blind to this because every failover test awaited post_with_failover
# directly. These smoke tests drive the REAL function bodies against a stub
# session.

@contextlib.contextmanager
def stub_upstream(session, bases=("http://primary",)):
    import functions

    orig_bases = functions.UPSTREAM_BASES
    orig_get_session = functions.get_session

    async def _stub_get_session():
        return session

    functions.UPSTREAM_BASES = list(bases)
    functions.get_session = _stub_get_session
    try:
        yield
    finally:
        functions.UPSTREAM_BASES = orig_bases
        functions.get_session = orig_get_session


def test_create_new_chat_awaits_failover_and_returns_session_id():
    import functions

    async def scenario():
        s = FakeSession([
            FakeResp(200, json_data={"data": {"biz_data": {"chat_session": {"id": "chat-new-1"}}}}),
        ])
        with stub_upstream(s):
            return await functions.create_new_chat("tok-1")

    assert asyncio.run(scenario()) == "chat-new-1"


def test_create_challange_pow_awaits_failover_and_returns_challenge():
    import functions

    challenge = {"challenge": "c", "salt": "s", "signature": "sig", "expire_at": 1, "difficulty": 0}

    async def scenario():
        s = FakeSession([
            FakeResp(200, json_data={"data": {"biz_data": {"challenge": challenge}}}),
        ])
        with stub_upstream(s):
            return await functions.create_challange_pow("/api/v0/chat/completion", "tok-1")

    assert asyncio.run(scenario()) == challenge


def test_upload_file_awaits_failover_and_yields_file_id():
    import functions

    async def scenario():
        s = FakeSession([
            FakeResp(200, json_data={"data": {"biz_data": {
                "id": "file-9", "status": "SUCCESS", "updated_at": 1726000000,
                "file_size": 3, "file_name": "a.txt",
            }}}),
        ])
        orig_pow = functions.solve_create_pow

        async def fake_pow(target_path, auth_token):
            return "pow-stub"

        functions.solve_create_pow = fake_pow
        try:
            with stub_upstream(s):
                events = [ev async for ev in functions.upload_file(b"abc", "a.txt", "text/plain", "tok-1")]
        finally:
            functions.solve_create_pow = orig_pow
        return events

    events = asyncio.run(scenario())
    assert events[0] == ("uploaded", "file-9")
    assert events[-1][0] == "success"


# ------------------------------------------------------------------------------
# PR #26 review (Blocker 2) — retry path must not self-deadlock on the chat lock

def test_handle_chat_retry_surrenders_lock_before_recursing():
    """The retry re-resolves the SAME upstream chat (the delete before the
    retry is simulated to miss the row — the divergence the old structure
    deadlocked on): the recursive handle_chat re-acquired the per-chat lock
    while the failing outer frame still owned it, so the request hung until
    the client gave up. handle_chat must complete instead of hanging, and the
    lock must be free afterwards."""
    import app as app_module

    async def scenario():
        app_module._chat_locks.clear()
        sig = "sig-retry-deadlock"
        session = {"token_id": "t1", "session_id": "chat-retry-1", "parent_message_id": 4}
        calls = {"send": 0}

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_find(s):
            return dict(session)  # SAME chat on every attempt — the deadlock shape

        def fake_get_token(tid):
            return {"token": "tok", "status": "ACTIVE"}

        def fake_send(chat_id, auth_token, message, parent, thinking=False, search=False, file_ids_=None):
            # send_message is an async-generator function: called un-awaited,
            # its body starts at the first __anext__ (inside _preflight_stream)
            calls["send"] += 1
            if calls["send"] == 1:
                raise Exception("HTTP 503: upstream down")

            async def gen():
                yield "hello "
                yield "world"

            return gen()

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        def fake_delete(tid, sid):
            pass  # delete misses the row -> the retry resolves to the same chat

        def fake_save(sig_, tid, sid, parent):
            pass

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
            ("delete_sessions_for_chat", fake_delete),
            ("save_session", fake_save),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: "FORMATTED"),
        ]
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            result = await asyncio.wait_for(
                app_module.handle_chat([{"role": "user", "content": "hi"}], "test-model"),
                timeout=10,
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)
        assert calls["send"] == 2, "the retry must actually run a second upstream attempt"
        lock = app_module._chat_lock("chat-retry-1")
        assert not lock.locked(), "no acquisition may survive the request"
        return result

    try:
        assert asyncio.run(scenario()) == "FORMATTED"
    except asyncio.TimeoutError:
        raise AssertionError("handle_chat retry deadlocked on the per-chat lock (Blocker 2)")
    finally:
        import app as app_module

        app_module._chat_locks.clear()


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
