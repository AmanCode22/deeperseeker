"""Regression tests for the rotating bounded retry (B10, Stage 1 audit).

The old empty-SSE retry re-entered handle_chat, which re-drew pick_token()'s
random pick — it could land on the SAME poisoned token/session, so the #33
symptom (repeated empty responses) persisted. The retry budget is now
MAX_UPSTREAM_ATTEMPTS attempts with jittered backoff, and each retry passes
the failed token id as an exclude set so pick_token() rotates off it.

Run:  python tests/test_retry_rotation.py   (pytest-compatible)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def _patch(patches):
    saved = [(name, getattr(app_module, name)) for name, _ in patches]
    for name, fn in patches:
        setattr(app_module, name, fn)
    return saved


def _restore(saved):
    for name, fn in saved:
        setattr(app_module, name, fn)


def test_retry_excludes_failed_token():
    """A generic upstream failure must retry on a token other than the one
    that failed (when another is available)."""
    calls = {"send": 0, "picks": []}
    sig = "sig-retry-rotate"

    async def scenario():
        app_module._chat_locks.clear()

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_pick(*a, **k):
            calls["picks"].append(k.get("exclude"))
            return 1 if calls["send"] == 0 else 2

        def fake_get_token(tid):
            return {"id": tid, "token": f"tok-{tid}", "status": "ACTIVE"}

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
                    raise RuntimeError(
                        "Empty response from DeepSeek (no parseable SSE content)"
                    )
                    yield ""  # pragma: no cover

                return fail_gen()

            async def ok_gen():
                yield "recovered on token 2"

            return ok_gen()

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),  # every attempt takes the create path
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("create_new_chat", lambda tok: _achat("chat-x")),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a: None),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = _patch(patches)
        try:
            return await app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "test-model"
            )
        finally:
            _restore(saved)
            app_module._chat_locks.clear()

    result = asyncio.run(scenario())
    assert result == "recovered on token 2"
    assert calls["send"] == 2
    assert calls["picks"][0] is None, "the first pick must not exclude anything"
    assert calls["picks"][1] == {1}, (
        f"the retry must exclude the failed token: {calls['picks']}"
    )


def test_retry_budget_is_bounded():
    """Persistent generic failures must stop at MAX_UPSTREAM_ATTEMPTS sends and
    return a 502-class error instead of retrying forever."""
    calls = {"send": 0, "picks": []}

    async def scenario():
        app_module._chat_locks.clear()

        async def fake_sig(messages, model, scope=""):
            return "sig-retry-bound"

        def fake_pick(*a, **k):
            calls["picks"].append(k.get("exclude"))
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
            calls["send"] += 1

            async def fail_gen():
                raise RuntimeError(
                    "Empty response from DeepSeek (no parseable SSE content)"
                )
                yield ""  # pragma: no cover

            return fail_gen()

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("create_new_chat", lambda tok: _achat("chat-y")),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a: None),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = _patch(patches)
        try:
            return await app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "test-model"
            )
        finally:
            _restore(saved)
            app_module._chat_locks.clear()

    result = asyncio.run(scenario())
    assert calls["send"] == app_module.MAX_UPSTREAM_ATTEMPTS, calls
    assert getattr(result, "status_code", None) == 502, result


async def _achat(session_id):
    return session_id


TESTS = [
    test_retry_excludes_failed_token,
    test_retry_budget_is_bounded,
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
