"""Audit regressions for the rate-discipline patch (issue #105).

The first pass of the rate-discipline work was reviewed again with fresh
eyes and a handful of defects turned up that the original twelve tests did
not cover. They are collected here, one block per defect, so a future change
that reopens any of them fails loudly:

  1. the rolling per-minute window is read from worker threads and written
     from the event loop, so its reads, writes and pruning must be mutually
     exclusive — an unguarded read-then-pop emptied a deque between the
     check and the pop and turned a burst into a 500;
  2. the 429 envelope must match the surface: Anthropic clients parse
     {"type": "error", ...}, OpenAI clients parse {"error": {...}};
  3. a conversation that moved to another account must be checked against
     THAT account's budget, not the one the session started on;
  4. the deep-health probe must tell "every account is cooling or over its
     budget" apart from "there is no account at all";
  5. the rotation path (the pool had nothing to hand out for an existing
     session) must answer 429 + Retry-After like every other path, not a
     bare 429 with no way to know when to come back;
  6. the 429 streak counter decides the cooldown rung and the parking
     threshold, so the increment and the read must be one atomic step —
     two concurrent 429s must not both see the same number;
  7. the file endpoints make upstream turns against an account, so they
     obey the same per-minute budget as a chat turn.

Everything here is offline: no upstream call, no network, no browser.

Run:  python tests/test_rate_discipline_audit.py   (pytest-compatible)
"""

import json
import os
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402


def _fresh_store(tokens=2):
    """Point functions at an isolated DB and seed it."""
    tmp = tempfile.mkdtemp()
    saved = functions._db
    functions._db = os.path.join(tmp, "deeperseeker.db")
    functions.init_db()
    for i in range(tokens):
        functions.add_token(f"tok-{i}", chr(ord("a") + i))
    return saved


def _streak_of(token_id, column="consecutive_429"):
    conn = functions.get_db()
    row = conn.execute(
        f"SELECT {column} FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def _cool_everyone(seconds=120):
    """Put every account in the pool into a cooling state."""
    future = time.time() + seconds
    conn = functions.get_db()
    conn.execute(
        "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=?",
        (future,),
    )
    conn.commit()
    conn.close()


def _achat(chat_id):
    """A minimal stand-in for the upstream chat handle."""

    async def _agen():
        return chat_id

    return _agen()


def _body(resp):
    """The JSON body of a response, whatever shape it comes back in."""
    raw = getattr(resp, "body", None)
    if raw is None:
        raise AssertionError(f"not a response object: {resp!r}")
    return json.loads(raw)


def _run_route(patches, is_anthropic=False, monkey=None):
    """Drive handle_chat with the store helpers patched out."""
    app_module._chat_locks.clear()
    saved_patches = [(name, getattr(app_module, name)) for name, _ in patches]
    for name, fn in patches:
        setattr(app_module, name, fn)
    try:
        import asyncio

        return asyncio.run(
            app_module.handle_chat(
                [{"role": "user", "content": "hi"}],
                "v4.1flash",
                is_anthropic=is_anthropic,
                scope="dsk-ratekey",
            )
        )
    finally:
        for name, fn in saved_patches:
            setattr(app_module, name, fn)
        app_module._chat_locks.clear()


def _route_patches(extra=None):
    """The patch set every route test needs (modeled on test_rate_discipline)."""

    async def fake_sig(messages, model, scope=""):
        return "sig-rate"

    def fake_get_token(tid):
        return {"id": tid, "token": "tok", "status": "ACTIVE", "alias": None}

    async def fake_files(messages, token, last_user_only=False):
        return []

    async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
        return "prompt"

    patches = [
        ("get_auth_token", lambda: "tok"),
        ("generate_signature", fake_sig),
        ("find_session", lambda s: None),
        ("get_token", fake_get_token),
        ("extract_and_upload_files", fake_files),
        ("build_prompt", fake_prompt),
        ("mark_limited", lambda *a, **k: None),
        ("mark_active", lambda *a, **k: None),
        ("delete_sessions_for_chat", lambda *a: None),
        ("save_session", lambda *a: None),
        ("record_file", lambda *a: None),
        ("parse_tools", lambda t: ([], t)),
    ]
    if extra:
        patches.extend(extra)
    return patches


# ---------------------------------------------------------------------------
# 1. the rolling window is shared between threads
# ---------------------------------------------------------------------------


def test_the_budget_window_survives_concurrent_pruning():
    """The window is written by the loop and read by worker threads.

    note_rate_send() prunes the window before it appends, and
    rate_budget_state() prunes it before it reads. Without mutual exclusion
    a reader can empty the deque between another thread's emptiness check
    and its popleft, which raises IndexError inside a worker thread and
    surfaces as a 500 on a live request.

    The collision is provoked on purpose: the window is filled with turns
    that are already outside the rolling window, so every reader has to pop
    them, and the switch interval is dropped so the interpreter hands the
    GIL over inside the pruning loop.
    """
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 100000
    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        errors = []
        stale = time.time() - 120

        def drain():
            try:
                functions.rate_budget_state(1)
            except Exception as exc:  # noqa: BLE001 - the point is to catch it
                errors.append(exc)

        for _ in range(20):
            for _ in range(3000):
                functions.note_rate_send(1, now=stale)
            threads = [threading.Thread(target=drain) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            assert not errors, f"the window was raced: {errors!r}"

        state = functions.rate_budget_state(1)
        assert state["used"] == 0, (
            f"every stale turn must have left the window: {state}"
        )
    finally:
        sys.setswitchinterval(old_interval)
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_the_budget_window_counts_every_concurrent_send():
    """A recorded send must never be lost to a concurrent reader."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 100000
    try:
        errors = []
        sends = []
        stop = time.time() + 0.3

        def writer():
            try:
                while time.time() < stop:
                    functions.note_rate_send(1)
                    sends.append(1)
            except Exception as exc:  # noqa: BLE001 - the point is to catch it
                errors.append(exc)

        def reader():
            try:
                while time.time() < stop:
                    functions.rate_budget_state(1)
            except Exception as exc:  # noqa: BLE001 - the point is to catch it
                errors.append(exc)

        threads = [threading.Thread(target=writer) for _ in range(3)]
        threads += [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"the window was raced: {errors!r}"
        state = functions.rate_budget_state(1)
        assert state["used"] == len(sends), (
            f"every recorded send must still be counted: {state} vs {len(sends)}"
        )
        # The limit is a threshold, not a cap on what the window may hold:
        # recording goes on, the refusal happens where the budget is read.
        assert state["exhausted"] is (state["used"] >= state["limit"]), state
        assert state["retry_after"] > 0, state
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


# ---------------------------------------------------------------------------
# 2. the 429 envelope must match the surface
# ---------------------------------------------------------------------------


def test_the_rate_refusal_uses_the_anthropic_envelope():
    """Anthropic clients parse {"type": "error", ...}, not {"error": ...}."""
    anthropic = app_module._rate_refusal(30, is_anthropic=True)
    assert anthropic.status_code == 429
    assert anthropic.headers.get("Retry-After") == "30"
    body = _body(anthropic)
    assert body["type"] == "error", body
    assert body["error"]["type"] == "rate_limit_error", body
    assert body["error"]["message"], body

    openai = app_module._rate_refusal(30)
    assert openai.status_code == 429
    body = _body(openai)
    assert body["error"]["type"] == "rate_limit_error", body
    assert "type" not in body, body


def test_the_anthropic_surface_answers_429_with_a_retry_after():
    """A Claude Code client must get the Anthropic shape and a wait time."""
    saved = _fresh_store()
    try:
        _cool_everyone(120)
        resp = _run_route(
            _route_patches(
                [
                    ("send_message", lambda *a, **k: None),
                    ("create_new_chat", lambda tok: _achat("chat-anthropic")),
                ]
            ),
            is_anthropic=True,
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        body = _body(resp)
        assert body["type"] == "error", body
        assert body["error"]["type"] == "rate_limit_error", body
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 3. a conversation that moved accounts is judged by the new account
# ---------------------------------------------------------------------------


def test_a_session_that_moved_accounts_is_refused_on_the_new_budget():
    """The budget checked is the one of the account that will do the work.

    A concurrent request can move the chat onto another account. The
    request that follows the move must be refused when THAT account is over
    its budget, even though the account the session started on still has
    room.
    """
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    try:
        # The account the chat moved to has already spent its minute.
        slot = functions.acquire_token_slot(2)
        slot.release()

        calls = {"find": 0}

        def fake_find_session(sig):
            calls["find"] += 1
            if calls["find"] == 1:
                return {
                    "token_id": 1,
                    "session_id": "chat-1",
                    "parent_message_id": 0,
                }
            return {"token_id": 2, "session_id": "chat-2", "parent_message_id": 0}

        sent = {"n": 0}

        def fake_send(*a, **k):
            sent["n"] += 1

            async def gen():
                yield "should not happen"

            return gen()

        resp = _run_route(
            _route_patches(
                [
                    ("find_session", fake_find_session),
                    ("send_message", fake_send),
                    ("create_new_chat", lambda tok: _achat("chat-moved")),
                ]
            )
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        assert sent["n"] == 0, (
            "the request must be refused before it reaches upstream"
        )
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


# ---------------------------------------------------------------------------
# 4. the deep-health probe names the condition it found
# ---------------------------------------------------------------------------


def test_the_deep_health_probe_names_the_rate_condition():
    """Cooling accounts and a missing pool are different problems."""
    import asyncio

    saved = _fresh_store()
    try:
        _cool_everyone(120)
        probe = asyncio.run(app_module._deep_health_probe())
        assert probe["upstream_reachable"] is False
        reason = probe["reason"]
        assert "no ACTIVE token" not in reason, (
            f"a cooling pool is not an empty pool: {reason!r}"
        )
        assert "cooling" in reason or "budget" in reason, reason
    finally:
        functions._db = saved

    saved = _fresh_store(tokens=0)
    try:
        probe = asyncio.run(app_module._deep_health_probe())
        assert probe["upstream_reachable"] is False
        assert probe["reason"] == "no ACTIVE token in pool", probe
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 5. the rotation path answers like every other path
# ---------------------------------------------------------------------------


def test_the_rotation_path_answers_429_with_a_retry_after():
    """An existing session whose account is cooling must get a wait time.

    This path used to answer with a hand-written 429 that carried no
    Retry-After and the OpenAI body on every surface, so a client had no
    way to know when to come back.
    """
    saved = _fresh_store()
    try:
        _cool_everyone(120)

        def fake_find_session(sig):
            return {"token_id": 1, "session_id": "chat-cool", "parent_message_id": 0}

        resp = _run_route(
            _route_patches(
                [
                    ("find_session", fake_find_session),
                    (
                        "get_token",
                        lambda tid: {
                            "id": tid,
                            "token": "tok",
                            "status": "RATE_LIMITED",
                            "alias": None,
                        },
                    ),
                    ("send_message", lambda *a, **k: None),
                    ("create_new_chat", lambda tok: _achat("chat-rot")),
                ]
            )
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), (
            "the rotation refusal must say when to come back"
        )
        body = _body(resp)
        assert body["error"]["type"] == "rate_limit_error", body
    finally:
        functions._db = saved


def test_the_rotation_path_uses_the_anthropic_envelope():
    saved = _fresh_store()
    try:
        _cool_everyone(120)

        def fake_find_session(sig):
            return {"token_id": 1, "session_id": "chat-cool", "parent_message_id": 0}

        resp = _run_route(
            _route_patches(
                [
                    ("find_session", fake_find_session),
                    (
                        "get_token",
                        lambda tid: {
                            "id": tid,
                            "token": "tok",
                            "status": "RATE_LIMITED",
                            "alias": None,
                        },
                    ),
                    ("send_message", lambda *a, **k: None),
                    ("create_new_chat", lambda tok: _achat("chat-rot-a")),
                ]
            ),
            is_anthropic=True,
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After")
        body = _body(resp)
        assert body["type"] == "error", body
        assert body["error"]["type"] == "rate_limit_error", body
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 6. the streak counter is incremented atomically
# ---------------------------------------------------------------------------


def test_the_429_streak_counter_is_atomic_under_concurrency():
    """Two concurrent 429s must not both read the same streak.

    The counter picks the rung of the cooldown ladder and decides whether
    the account is parked. Read back with a separate SELECT, a concurrent
    increment could land in between: both callers would then see the higher
    number, the ladder would skip a rung and the account could be parked one
    strike too early.
    """
    saved = _fresh_store()
    try:
        results = []
        guard = threading.Lock()

        def bump():
            value = functions._bump_429_streak(1)
            with guard:
                results.append(value)

        threads = [threading.Thread(target=bump) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == list(range(1, 9)), (
            f"every caller must see its own increment, got {sorted(results)}"
        )
        assert _streak_of(1) == 8, _streak_of(1)
    finally:
        functions._db = saved


def test_the_403_streak_counter_is_atomic_under_concurrency():
    """The 403 counter drives the quarantine, so it must be atomic too."""
    saved = _fresh_store()
    try:
        results = []
        guard = threading.Lock()

        def bump():
            value = functions._bump_403_streak(1)
            with guard:
                results.append(value)

        threads = [threading.Thread(target=bump) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert sorted(results) == list(range(1, 7)), sorted(results)
        assert _streak_of(1, "consecutive_403") == 6
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 7. the file endpoints obey the same budget
# ---------------------------------------------------------------------------


class _FakeUpload:
    """A stand-in for an uploaded file in a multipart form."""

    def __init__(self, data=b"hello", name="note.txt", content_type="text/plain"):
        self._data = data
        self.filename = name
        self.content_type = content_type

    async def read(self, size=-1):
        return self._data


class _FakeURL:
    def __init__(self, path):
        self.path = path


class _FakeRequest:
    def __init__(self, upload=None, path="/v1/files"):
        self._upload = upload
        self.url = _FakeURL(path)

    async def form(self):
        return {"file": self._upload}


def _run_upload(upload_impl, request=None):
    """Drive files_upload with the upstream mocked out."""
    import asyncio

    patches = [
        ("check_key", lambda req: True),
        ("pick_token", lambda *a, **k: 1),
        (
            "get_token",
            lambda tid: {"id": tid, "token": "tok", "status": "ACTIVE", "alias": None},
        ),
        ("upload_file", upload_impl),
    ]
    saved_patches = [(name, getattr(app_module, name)) for name, _ in patches]
    for name, fn in patches:
        setattr(app_module, name, fn)
    try:
        return asyncio.run(
            app_module.files_upload(request or _FakeRequest(_FakeUpload()))
        )
    finally:
        for name, fn in saved_patches:
            setattr(app_module, name, fn)


def _fake_upload_file(calls):
    async def upload_impl(data, filename, content_type, token):
        calls.append(token)
        yield "success", {
            "file_id": "file-1",
            "size": len(data),
            "anthropic_timestamp": 0,
            "openai_timestamp": 0,
        }

    return upload_impl


def test_a_file_upload_over_the_budget_is_refused_before_upstream():
    """An upload is an upstream turn: it must not outrun the account."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    try:
        slot = functions.acquire_token_slot(1)
        slot.release()

        calls = []
        resp = _run_upload(_fake_upload_file(calls))
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        assert calls == [], "the upload must be refused before it reaches upstream"
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_a_file_upload_spends_the_account_budget():
    """The upload is debited, so the budget counts every upstream turn."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 10
    try:
        calls = []
        resp = _run_upload(_fake_upload_file(calls))
        assert isinstance(resp, dict), resp
        assert calls == ["tok"], calls
        state = functions.rate_budget_state(1)
        assert state["used"] == 1, state
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_a_file_content_fetch_over_the_budget_is_refused():
    """The download is an upstream turn against the owning account."""
    import asyncio

    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    try:
        slot = functions.acquire_token_slot(1)
        slot.release()

        calls = []

        async def fake_get_file_content(fetch_token, fid):
            calls.append(fetch_token)
            yield "text/plain"
            yield b"data"

        patches = [
            ("check_key", lambda req: True),
            ("pick_token", lambda *a, **k: 1),
            (
                "get_token",
                lambda tid: {
                    "id": tid,
                    "token": "tok",
                    "status": "ACTIVE",
                    "alias": None,
                },
            ),
            ("get_file_token", lambda fid: None),
            ("get_file_content", fake_get_file_content),
        ]
        saved_patches = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            resp = asyncio.run(app_module.files_content("file-1", None))
        finally:
            for name, fn in saved_patches:
                setattr(app_module, name, fn)

        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        assert calls == [], "the fetch must be refused before it reaches upstream"
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


TESTS = [
    test_the_budget_window_survives_concurrent_pruning,
    test_the_budget_window_counts_every_concurrent_send,
    test_the_rate_refusal_uses_the_anthropic_envelope,
    test_the_anthropic_surface_answers_429_with_a_retry_after,
    test_a_session_that_moved_accounts_is_refused_on_the_new_budget,
    test_the_deep_health_probe_names_the_rate_condition,
    test_the_rotation_path_answers_429_with_a_retry_after,
    test_the_rotation_path_uses_the_anthropic_envelope,
    test_the_429_streak_counter_is_atomic_under_concurrency,
    test_the_403_streak_counter_is_atomic_under_concurrency,
    test_a_file_upload_over_the_budget_is_refused_before_upstream,
    test_a_file_upload_spends_the_account_budget,
    test_a_file_content_fetch_over_the_budget_is_refused,
]


if __name__ == "__main__":
    for _test in TESTS:
        _test()
        print(f"ok: {_test.__name__}")
    print(f"ok: {len(TESTS)} rate-discipline audit tests")
