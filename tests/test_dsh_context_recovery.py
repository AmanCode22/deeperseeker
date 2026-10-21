"""Regression tests for the DSH context-recovery fix.

The deepseek-harness failure class this file locks shut:

  1. an oversized single message (a harness replaying its whole context) used
     to fail upstream with a hard rejection and then fail identically on every
     retry — the payload was never shrunk. It is now capped (tail kept).
  2. a context-class failure on a continuing session used to retry as a lossy
     24K history reset (or not at all, once the budget died). The retry now
     FORCES a summary rollover — full continuity in a fresh chat — even when
     the client-side estimate says the context is fine.
  3. the rollover estimate now includes tool schemas + prompt overhead, so
     proactive rollover fires before the upstream session dies instead of
     after.

Run:  python tests/test_dsh_context_recovery.py   (pytest-compatible)
"""

import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import plugin_helper  # noqa: E402
from functions import UpstreamError, is_context_limit_error  # noqa: E402
from plugin_helper import build_prompt, needs_rollover  # noqa: E402


def big_text(words):
    return " ".join([f"tokenword{i}" for i in range(words)])


# --------------------------------------------------------------------------
# 1. the user-message cap
# --------------------------------------------------------------------------


def test_first_message_user_text_is_capped():
    """A message over the cap must not reach upstream raw: it gets
    tail-truncated with an explicit marker, under the knob."""
    with mock.patch.object(plugin_helper, "MAX_USER_MESSAGE_TOKENS", 1000):
        huge = big_text(1500)  # ~4-5x the patched cap
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": huge},
        ]
        prompt = asyncio.run(
            build_prompt(msgs, [], "test-model", is_first_message=True)
        )
    assert "[USER]" in prompt
    # tail kept, head dropped: the LAST words of the message survive
    assert "tokenword1499" in prompt, "the newest content must be kept"
    assert "truncated to fit the context limit" in prompt
    # the capped [USER] block must sit under the knob's budget (with slack
    # for the marker line)
    from plugin_helper import count_tokens as _ct

    user_block = prompt.split("[USER]\n", 1)[1]
    assert _ct(user_block) <= 1000 + 2000, _ct(user_block)


def test_oversized_message_under_ceiling_untouched():
    """Below the cap the first-message path is byte-identical to before — the
    cap must never bite normal traffic (claude desktop, small harnesses)."""
    small = "hello " * 100
    msgs = [{"role": "user", "content": small}]
    prompt = asyncio.run(build_prompt(msgs, [], "test-model", is_first_message=True))
    assert "truncated" not in prompt
    assert small.strip() in prompt


def test_continuation_trailing_user_text_is_capped():
    """A huge trailing user message on a continuing chat is capped too."""
    with mock.patch.object(plugin_helper, "MAX_USER_MESSAGE_TOKENS", 1000):
        huge = big_text(1500)
        msgs = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": huge},
        ]
        prompt = asyncio.run(
            build_prompt(msgs, [], "test-model", is_first_message=False)
        )
    assert "[USER]" in prompt
    assert "truncated to fit the context limit" in prompt


# --------------------------------------------------------------------------
# 2. the context classifier
# --------------------------------------------------------------------------


def test_classifier_matches_context_class_failures():
    assert is_context_limit_error(
        UpstreamError(400, "This model's maximum context length is 393228 tokens")
    )
    assert is_context_limit_error(
        UpstreamError(
            502,
            "Empty response from DeepSeek (prompt may exceed the session context limit)",
        )
    )
    # our own app-level message (raised as a plain UpstreamError)
    assert is_context_limit_error(UpstreamError(502, "context limit exceeded"))
    # non-context failures stay retry-shaped
    assert not is_context_limit_error(UpstreamError(502, "upstream timeout"))
    assert not is_context_limit_error(UpstreamError(429, "rate limited"))
    assert not is_context_limit_error(TimeoutError())
    # loose words like 'exceeded' only count for real UpstreamError bodies
    assert not is_context_limit_error(RuntimeError("deadline exceeded"))
    # review fix (PR #67): the loose words count only when ANCHORED to
    # context material — a bare 'exceed' in an upstream body, a 'too long'
    # with no prompt/message in front, and a plain-exception 'context'
    # substring are retry-shaped, not context-shaped
    assert not is_context_limit_error(UpstreamError(400, "header limit exceeded"))
    assert not is_context_limit_error(UpstreamError(400, "request line too long"))
    assert not is_context_limit_error(RuntimeError("context deadline exceeded"))
    # ... while the anchored forms still classify
    assert is_context_limit_error(
        UpstreamError(400, "the last message is too long for the context window")
    )
    assert is_context_limit_error(
        UpstreamError(400, "prompt exceeded the context window")
    )
    assert is_context_limit_error(RuntimeError("maximum context length is 65536"))
    assert is_context_limit_error(RuntimeError("token limit hit"))


# --------------------------------------------------------------------------
# 3. error-driven rollover inside handle_chat
# --------------------------------------------------------------------------


def test_context_failure_retries_with_forced_summary_rollover():
    """A continuing session that dies with a context-class failure must come
    back on a FRESH chat seeded with a handoff summary — even though the
    client-side estimate is far below the rollover threshold."""
    sends = {"n": 0}
    prompts = []
    sig = "sig-dsh-ctx"

    async def scenario():
        app_module._chat_locks.clear()

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_pick(*a, **k):
            return 1

        def fake_get_token(tid):
            return {"id": tid, "token": "tok", "status": "ACTIVE"}

        def fake_send(
            chat_id,
            auth_token,
            message,
            parent,
            thinking=False,
            search=False,
            file_ids_=None,
        ):
            sends["n"] += 1
            prompts.append(message)
            if sends["n"] == 1:
                # the remembered chat is over the upstream context limit
                async def ctx_fail():
                    raise UpstreamError(
                        502,
                        "Empty response from DeepSeek (prompt may exceed the session context limit)",
                    )
                    yield ""  # pragma: no cover

                return ctx_fail()
            if sends["n"] == 2:
                # the summary request in the scratch chat
                async def summary_gen():
                    yield "[SUMMARY] the conversation so far: user is building a bridge"

                return summary_gen()

            async def ok_gen():
                yield "continued after rollover"

            return ok_gen()

        async def fake_create(chat_token):
            return f"chat-{sends['n']}"

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return f"prompt(first={is_first}, summary={bool(rollover_summary)})"

        # the stored session: a continuing chat (parent != 0). Mirrors the
        # real store: save_session writes, delete_sessions_for_chat clears —
        # the retry must see the DELETED state, not a stale snapshot.
        store = {sig: {"token_id": 1, "session_id": "chat-0", "parent_message_id": 42}}

        def fake_find(s):
            return store.get(s)

        def fake_delete(token_id, session_id):
            # sync on purpose: delete_sessions_for_chat runs via _db()
            # (asyncio.to_thread) — an async fake here would create a
            # coroutine that is never awaited (review fix, PR #58-#67
            # test-hygiene) and its body would never run.
            for key in [
                k for k, v in store.items() if v.get("session_id") == session_id
            ]:
                store.pop(key, None)
            return None

        def fake_save(s, token_id, session_id, parent=0):
            store[s] = {
                "token_id": token_id,
                "session_id": session_id,
                "parent_message_id": parent,
            }
            return None

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", fake_find),
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("create_new_chat", fake_create),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", fake_delete),
            ("save_session", fake_save),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await app_module.handle_chat(
                [
                    {"role": "user", "content": "start"},
                    {"role": "assistant", "content": "ok"},
                    {"role": "user", "content": "next turn"},
                ],
                "test-model",
                scope="dsk-dsh",
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)
            app_module._chat_locks.clear()

    result = asyncio.run(scenario())
    assert result == "continued after rollover", result
    # attempt 1 failed on the remembered chat; attempt 2 asked for the summary
    # in a scratch chat; attempt 3 sent the seeded prompt into a fresh chat
    assert sends["n"] == 3, sends
    assert "[SUMMARY]" in prompts[1], "attempt 2 must be the summary request"
    assert "summary=True" in prompts[2], (
        "the retry must seed the fresh chat with the handoff summary"
    )


def test_non_context_failure_keeps_plain_retry():
    """Generic failures retry as before — no forced summary, no behavior
    change for the non-context failure classes."""
    sends = {"n": 0}
    prompts = []
    sig = "sig-dsh-generic"

    async def scenario():
        app_module._chat_locks.clear()

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_pick(*a, **k):
            return 1

        def fake_get_token(tid):
            return {"id": tid, "token": "tok", "status": "ACTIVE"}

        def fake_send(
            chat_id,
            auth_token,
            message,
            parent,
            thinking=False,
            search=False,
            file_ids_=None,
        ):
            sends["n"] += 1
            prompts.append(message)
            if sends["n"] == 1:

                async def boom():
                    raise UpstreamError(502, "upstream exploded (storage shard down)")
                    yield ""  # pragma: no cover

                return boom()

            async def ok_gen():
                yield "recovered"

            return ok_gen()

        async def fake_create(chat_token):
            return f"chat-{sends['n']}"

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return f"prompt(first={is_first}, summary={bool(rollover_summary)})"

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("create_new_chat", fake_create),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a, **k: None),
            ("record_file", lambda *a: None),
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
            app_module._chat_locks.clear()

    result = asyncio.run(scenario())
    assert result == "recovered"
    assert sends["n"] == 2
    assert "summary=False" in prompts[1], (
        "a generic failure must NOT force a summary rollover"
    )


# --------------------------------------------------------------------------
# 4. the overhead-aware rollover estimate
# --------------------------------------------------------------------------


def test_tools_push_the_estimate_over_the_limit():
    """A conversation just under the limit stays under without tools and
    crosses it with a real tool-schema block — proactive rollover must fire
    BEFORE the upstream session dies, not after."""
    msgs = [
        {"role": "user", "content": big_text(60)},
        {"role": "assistant", "content": big_text(60)},
        {"role": "user", "content": big_text(60)},
    ]
    small_tools = [{"type": "function", "function": {"name": "t", "parameters": {}}}]
    with (
        mock.patch.object(plugin_helper, "OBSERVED_MEMORY_LIMIT_TOKENS", 1000),
        mock.patch.object(plugin_helper, "ROLLOVER_SAFETY_TOKENS", 10),
        mock.patch.object(plugin_helper, "PROMPT_OVERHEAD_TOKENS", 0),
    ):
        # without tools: under the (patched) threshold
        assert not needs_rollover(msgs, small_tools)
        # with a big tool schema: over it
        big_tools = [
            {
                "type": "function",
                "function": {"name": f"tool_{n}", "parameters": big_text(200)},
            }
            for n in range(4)
        ]
        assert needs_rollover(msgs, big_tools)


def test_first_exchange_still_never_rolls_over():
    """The cap changed nothing about the first-exchange policy (issue #22)."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": big_text(320000)},
    ]
    assert not needs_rollover(msgs)


TESTS = [
    test_first_message_user_text_is_capped,
    test_oversized_message_under_ceiling_untouched,
    test_continuation_trailing_user_text_is_capped,
    test_classifier_matches_context_class_failures,
    test_context_failure_retries_with_forced_summary_rollover,
    test_non_context_failure_keeps_plain_retry,
    test_tools_push_the_estimate_over_the_limit,
    test_first_exchange_still_never_rolls_over,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            import traceback

            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
