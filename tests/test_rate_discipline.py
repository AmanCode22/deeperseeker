"""Regression tests for the rate discipline added for issue #105.

Issue #105 traced a kill-chain: an account that upstream had refused with
"messages too frequent" was handed straight back out by the pool's
all-cooling fallback a minute later, kept being refused, and was revoked
(40003) minutes later with no upstream traffic in between. The fix has four
parts, one test block each:

  1. the cooldown GROWS with the 429 streak (60 -> 300 -> 900 -> 3600 s) and
     a successful turn resets the streak, so one bad minute does not
     condemn an account;
  2. after enough consecutive 429s the account is parked instead of cooled;
  3. the pool REFUSES to hand out an account that is still cooling, and the
     routes turn that refusal into a 429 with a Retry-After instead of a 503;
  4. every account has a rolling per-minute request budget, and a request
     that would exceed it is refused with 429 + Retry-After BEFORE it
     reaches upstream.

Everything here is offline: no upstream call, no network, no browser.

Run:  python tests/test_rate_discipline.py   (pytest-compatible)
"""

import asyncio
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402


def _fresh_store():
    """Point functions at an isolated DB and seed one token."""
    tmp = tempfile.mkdtemp()
    saved = functions._db
    functions._db = os.path.join(tmp, "deeperseeker.db")
    functions.init_db()
    functions.add_token("tok-a", "a")
    functions.add_token("tok-b", "b")
    return saved


def _streak_of(token_id):
    """The 429 streak straight from the store (get_token() deliberately does
    not expose the streak columns — the pool view is their only reader)."""
    conn = functions.get_db()
    row = conn.execute(
        "SELECT consecutive_429 FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    return row[0] if row else 0


def _cooldown_of(token_id):
    conn = functions.get_db()
    row = conn.execute(
        "SELECT status, rate_limited_until FROM tokens WHERE id = ?", (token_id,)
    ).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# 1. the 429 ladder
# ---------------------------------------------------------------------------


def test_429_cooldown_grows_with_the_streak():
    """Each consecutive 429 rests the account for longer than the last one."""
    saved = _fresh_store()
    try:
        expected = [60, 300, 900, 3600]
        for streak, want in enumerate(expected, start=1):
            functions.mark_limited(1, 429)
            status, until = _cooldown_of(1)
            assert status == "RATE_LIMITED", (streak, status)
            got = int(round(until - time.time()))
            assert 0 < got <= want + 2, (
                f"streak {streak}: expected a cooldown of about {want}s, got {got}s"
            )
            assert got > want - 5, (
                f"streak {streak}: expected a cooldown of about {want}s, got {got}s"
            )
            # The streak counter is what drives the ladder.
            assert _streak_of(1) == streak
    finally:
        functions._db = saved


def test_a_successful_turn_resets_the_429_streak():
    """One good turn puts the account back on the first rung."""
    saved = _fresh_store()
    try:
        functions.mark_limited(1, 429)
        functions.mark_limited(1, 429)
        assert _streak_of(1) == 2

        functions.mark_active(1)
        assert _streak_of(1) == 0, (
            "a successful turn must clear the 429 streak"
        )

        functions.mark_limited(1, 429)
        status, until = _cooldown_of(1)
        got = int(round(until - time.time()))
        assert got < 120, (
            f"after a success the ladder must restart at the first rung, got {got}s"
        )
    finally:
        functions._db = saved


def test_restore_clears_the_429_streak():
    saved = _fresh_store()
    try:
        functions.mark_limited(1, 429)
        functions.mark_limited(1, 429)
        functions.restore_token(1)
        assert _streak_of(1) == 0
        assert functions.get_token(1)["status"] == "ACTIVE"
    finally:
        functions._db = saved


def test_repeated_429s_park_the_account():
    """A long streak is a rate strike, not bad luck: the account is parked."""
    saved = _fresh_store()
    try:
        for _ in range(functions.RATE_LIMIT_PARK_AFTER):
            functions.mark_limited(1, 429)
        status, until = _cooldown_of(1)
        assert status == "PARKED", (
            f"after {functions.RATE_LIMIT_PARK_AFTER} consecutive 429s the account "
            f"must be parked, got {status}"
        )
        assert until and until > time.time(), "a parked account needs a return time"
    finally:
        functions._db = saved


def test_an_upstream_retry_after_is_never_shorter_than_the_rung():
    """Upstream may ask for a longer rest; it may not ask for a shorter one."""
    saved = _fresh_store()
    try:
        functions.mark_limited(1, 429, 900)
        status, until = _cooldown_of(1)
        got = int(round(until - time.time()))
        assert got >= 900, f"an upstream Retry-After of 900s must be honoured, got {got}s"
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 2. the pool refuses to hand out a cooling account
# ---------------------------------------------------------------------------


def test_pool_refuses_when_every_account_is_cooling():
    """The hammer-through fallback is gone (that fallback is the #105 chain)."""
    saved = _fresh_store()
    try:
        future = time.time() + 300
        conn = functions.get_db()
        conn.execute(
            "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=?",
            (future,),
        )
        conn.commit()
        conn.close()

        assert functions.pick_token() is None, (
            "a cooling account must not be handed back out"
        )
        limited, retry_after = functions.pool_rate_state()
        assert limited is True, "the pool must report itself as rate-limited"
        assert 0 < retry_after <= 300, retry_after
    finally:
        functions._db = saved


def test_the_old_fallback_is_one_env_var_away():
    saved = _fresh_store()
    try:
        future = time.time() + 300
        conn = functions.get_db()
        conn.execute(
            "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=?",
            (future,),
        )
        conn.commit()
        conn.close()

        functions.REFUSE_WHEN_ALL_COOLING = False
        try:
            assert functions.pick_token() is not None, (
                "with DEEPSEEKER_REFUSE_WHEN_ALL_COOLING=0 the old "
                "soonest-to-recover fallback must return"
            )
        finally:
            functions.REFUSE_WHEN_ALL_COOLING = True
    finally:
        functions._db = saved


def test_a_quarantined_pool_is_not_a_rate_condition():
    """Quarantine is an operator action, so it keeps the old 503 answer."""
    saved = _fresh_store()
    try:
        functions.mark_quarantined(1, "test")
        functions.mark_quarantined(2, "test")
        limited, _retry_after = functions.pool_rate_state()
        assert limited is False, (
            "an all-quarantined pool is a configuration problem, not a rate one"
        )
    finally:
        functions._db = saved


# ---------------------------------------------------------------------------
# 3. the per-minute request budget
# ---------------------------------------------------------------------------


def test_the_budget_is_spent_by_upstream_turns():
    """Every reserved slot is one upstream turn against that account."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 3
    try:
        for _ in range(3):
            functions.acquire_token_slot(1)

        state = functions.rate_budget_state(1)
        assert state["used"] == 3, state
        assert state["exhausted"] is True, state
        assert state["retry_after"] > 0, "the caller needs to know how long to wait"

        # Another account is unaffected: the budget is per token.
        assert functions.rate_budget_state(2)["exhausted"] is False

        # The window is rolling: 61 s later the oldest turns have left it.
        later = functions.rate_budget_state(1, now=time.time() + 61)
        assert later["used"] == 0, later
        assert later["exhausted"] is False, later
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_the_budget_makes_the_scheduler_prefer_a_fresh_account():
    """With two accounts, the one that still has budget wins the pick."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    try:
        for _ in range(1):
            functions.acquire_token_slot(1)
        # Token 2 is untouched and must be preferred over the spent token 1.
        assert functions.pick_token() == 2, (
            "an account over its budget must sort behind one that still has room"
        )
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_the_budget_is_opt_in():
    """Unset means today's behaviour: no cap, no refusal.

    Issue #105 asks for an OPT-IN cap ("unset = current behaviour"). A cap
    that starts refusing requests the day it lands changes what every
    existing deployment does, which is not what the reporter asked for.
    """
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 0
    try:
        for _ in range(50):
            functions.acquire_token_slot(1)

        state = functions.rate_budget_state(1)
        assert state["exhausted"] is False, state
        assert state["retry_after"] == 0, state
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_the_budget_ships_off_by_default():
    """The shipped default is opt-in, not a cap every deployment inherits."""
    if os.getenv("DEEPSEEKER_TOKEN_MAX_MSGS_PER_MIN", "").strip():
        return  # the operator turned it on; the default is not in play
    assert functions.TOKEN_MAX_MSGS_PER_MIN == 0, (
        "DEEPSEEKER_TOKEN_MAX_MSGS_PER_MIN must ship unset (=0, no cap); "
        f"the default is {functions.TOKEN_MAX_MSGS_PER_MIN}"
    )


def test_the_budget_waits_for_the_window_instead_of_failing():
    """#105: the cap waits rather than fails.

    An agent turn is a burst of upstream messages; the burst should ride
    out the rolling window instead of the client being told to come back.
    The wait happens BEFORE the upstream call, so it costs the account
    nothing.
    """
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    saved_wait = app_module.TOKEN_MAX_WAIT_SECONDS
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    app_module.TOKEN_MAX_WAIT_SECONDS = 5
    try:
        # One send, almost out of the window: it frees a slot in ~1 s.
        functions.note_rate_send(1, now=time.time() - 59)

        sent = {"n": 0}

        def fake_send(*a, **k):
            sent["n"] += 1

            async def gen():
                yield "ok"

            return gen()

        started = time.time()
        resp = _run_route(
            _route_patches(
                [
                    ("pick_token", lambda *a, **k: 1),
                    ("send_message", fake_send),
                    ("create_new_chat", lambda tok: _achat("chat-wait")),
                    ("format_response", lambda text, model, messages, tools=None: text),
                ]
            )
        )
        waited = time.time() - started
        assert getattr(resp, "status_code", None) != 429, (
            "the request must WAIT for the window, not be refused"
        )
        assert sent["n"] == 1, "the request must go out after the wait"
        assert waited >= 0.5, f"the request must actually wait, waited {waited:.2f}s"
    finally:
        app_module.TOKEN_MAX_WAIT_SECONDS = saved_wait
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_a_wait_longer_than_the_cap_is_refused_instead():
    """The wait is bounded: a request never blocks for minutes."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    saved_wait = app_module.TOKEN_MAX_WAIT_SECONDS
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    app_module.TOKEN_MAX_WAIT_SECONDS = 1
    try:
        # This send only leaves the window in ~30 s, far beyond the cap.
        functions.note_rate_send(1, now=time.time() - 30)

        sent = {"n": 0}

        def fake_send(*a, **k):
            sent["n"] += 1

            async def gen():
                yield "ok"

            return gen()

        started = time.time()
        resp = _run_route(
            _route_patches(
                [
                    ("pick_token", lambda *a, **k: 1),
                    ("send_message", fake_send),
                    ("create_new_chat", lambda tok: _achat("chat-wait-cap")),
                    ("format_response", lambda text, model, messages, tools=None: text),
                ]
            )
        )
        elapsed = time.time() - started
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        assert sent["n"] == 0, "the request must not reach upstream"
        assert elapsed < 5, f"the refusal must be prompt, took {elapsed:.2f}s"
    finally:
        app_module.TOKEN_MAX_WAIT_SECONDS = saved_wait
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


# ---------------------------------------------------------------------------
# 4. the routes: 429 with a Retry-After, never a silent stampede
# ---------------------------------------------------------------------------


def _route_patches(extra=None):
    """The patch set every route test needs (modeled on test_usage_stats)."""

    async def fake_sig(messages, model, scope=""):
        return "sig-rate"

    def fake_get_token(tid):
        return {"id": tid, "token": "tok", "status": "ACTIVE"}

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


def _run_route(patches, monkey=None):
    app_module._chat_locks.clear()
    saved_patches = [(name, getattr(app_module, name)) for name, _ in patches]
    for name, fn in patches:
        setattr(app_module, name, fn)
    try:
        return asyncio.run(
            app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "v4.1flash", scope="dsk-ratekey"
            )
        )
    finally:
        for name, fn in saved_patches:
            setattr(app_module, name, fn)
        app_module._chat_locks.clear()


def test_a_request_over_the_budget_is_refused_before_upstream():
    """429 + Retry-After, and the upstream is never touched."""
    saved = _fresh_store()
    saved_limit = functions.TOKEN_MAX_MSGS_PER_MIN
    functions.TOKEN_MAX_MSGS_PER_MIN = 1
    try:
        sent = {"n": 0}

        def fake_send(*a, **k):
            sent["n"] += 1

            async def gen():
                yield "should not happen"

            return gen()

        for _ in range(1):
            functions.acquire_token_slot(1)

        resp = _run_route(
            _route_patches(
                [
                    ("pick_token", lambda *a, **k: 1),
                    ("send_message", fake_send),
                    ("create_new_chat", lambda tok: _achat("chat-rate")),
                    ("format_response", lambda text, model, messages, tools=None: text),
                ]
            )
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
        assert sent["n"] == 0, "the request must be refused BEFORE it reaches upstream"
    finally:
        functions.TOKEN_MAX_MSGS_PER_MIN = saved_limit
        functions._db = saved


def test_a_cooling_pool_answers_429_not_503():
    """Every account cooling is a rate condition, so the client is told to wait."""
    saved = _fresh_store()
    try:
        future = time.time() + 120
        conn = functions.get_db()
        conn.execute(
            "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=?",
            (future,),
        )
        conn.commit()
        conn.close()

        resp = _run_route(
            _route_patches(
                [
                    ("send_message", lambda *a, **k: None),
                    ("create_new_chat", lambda tok: _achat("chat-rate-cooling")),
                    ("format_response", lambda text, model, messages, tools=None: text),
                ]
            )
        )
        assert getattr(resp, "status_code", None) == 429, resp
        assert resp.headers.get("Retry-After"), "a 429 must say when to come back"
    finally:
        functions._db = saved


def _achat(chat_id):
    """A minimal stand-in for the upstream chat handle."""

    async def _agen():
        return chat_id

    return _agen()


TESTS = [
    test_429_cooldown_grows_with_the_streak,
    test_a_successful_turn_resets_the_429_streak,
    test_restore_clears_the_429_streak,
    test_repeated_429s_park_the_account,
    test_an_upstream_retry_after_is_never_shorter_than_the_rung,
    test_pool_refuses_when_every_account_is_cooling,
    test_the_old_fallback_is_one_env_var_away,
    test_a_quarantined_pool_is_not_a_rate_condition,
    test_the_budget_is_spent_by_upstream_turns,
    test_the_budget_makes_the_scheduler_prefer_a_fresh_account,
    test_the_budget_is_opt_in,
    test_the_budget_ships_off_by_default,
    test_the_budget_waits_for_the_window_instead_of_failing,
    test_a_wait_longer_than_the_cap_is_refused_instead,
    test_a_request_over_the_budget_is_refused_before_upstream,
    test_a_cooling_pool_answers_429_not_503,
]


if __name__ == "__main__":
    for _test in TESTS:
        _test()
        print(f"ok: {_test.__name__}")
    print(f"ok: {len(TESTS)} rate-discipline tests")
