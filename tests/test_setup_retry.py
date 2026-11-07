"""Regression tests for auth failures on the turn-PREPARATION hops.

`create_new_chat` runs before the try block that owns token rotation, so an
upstream 401 there (HTTP 200 + `code=40003 INVALID_TOKEN`, which
`_biz_data` turns into `UpstreamBizError(401)`) used to escape handle_chat
entirely: the client got a bare 500 and the dead token was never marked, so
it kept being picked and every later turn failed the same way.

The retry must NOT recurse while the create path still holds `_sig_locks[sig]`
— asyncio.Lock is not reentrant, so that variant hung the request instead of
rotating it.

Run:  python -m pytest tests/test_setup_retry.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402


def _run_create_path_scenario():
    """First create_new_chat raises 401; the turn must rotate and succeed."""
    calls = {"create": 0, "limited": [], "picked": [], "send": 0}
    sessions = {}

    async def fake_sig(messages, model, scope=""):
        return "sig-setup-retry"

    def fake_find(s):
        return sessions.get(s)

    def fake_pick(exclude=None):
        calls["picked"].append(set(exclude or ()))
        return 2 if exclude and 1 in exclude else 1

    def fake_get_token(tid):
        return {"id": tid, "token": f"tok-{tid}", "status": "ACTIVE", "alias": f"t{tid}"}

    async def fake_create_chat(token):
        calls["create"] += 1
        if calls["create"] == 1:
            raise functions.UpstreamBizError(401, "chat_session/create: code=40003 msg=INVALID_TOKEN")
        return f"chat-{token}"

    def fake_send(chat_id, auth_token, message, parent, thinking=False, search=False, file_ids_=None):
        calls["send"] += 1

        async def ok_gen():
            yield "recovered after setup rotation"

        return ok_gen()

    async def fake_files(messages, token, last_user_only=False):
        return []

    async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
        return "prompt"

    def fake_save(sig, token_id, session_id, parent):
        sessions[sig] = {
            "token_id": token_id,
            "session_id": session_id,
            "parent_message_id": parent,
        }

    def fake_limited(tid, code=None):
        calls["limited"].append((tid, code))

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
        ("save_session", fake_save),
        ("parse_tools", lambda t: ([], t)),
        ("format_response", lambda text, model, messages, tools=None: text),
        ("needs_rollover", lambda messages, tools: False),
    ]

    async def run():
        app_module._chat_locks.clear()
        app_module._sig_locks.clear()
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            # A hard timeout turns the old deadlock into a failure, not a hang.
            return await asyncio.wait_for(
                app_module.handle_chat(
                    [{"role": "user", "content": "hi"}], "test-model"
                ),
                timeout=10,
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    return asyncio.run(run()), calls


def test_create_path_401_rotates_instead_of_500():
    result, calls = _run_create_path_scenario()

    assert result == "recovered after setup rotation", result
    # The dead token was marked (401 -> quarantine door) ...
    assert calls["limited"] == [(1, 401)], calls["limited"]
    # ... and the retry went to a different account, not the same one again.
    assert 1 in calls["picked"][-1], calls["picked"]
    assert calls["send"] == 1


def test_create_path_401_does_not_deadlock_on_the_signature_lock():
    """The retry re-enters handle_chat, which takes _sig_locks[sig] again."""
    # If the create-path retry recursed while still holding the sig lock, the
    # scenario's wait_for would time out; reaching past it proves the release.
    _run_create_path_scenario()
    for sig, lock in app_module._sig_locks.items():
        assert not lock.locked(), f"signature lock {sig!r} left held"


def test_create_path_waf_202_rotates_and_recovers():
    """When create_new_chat hits an AWS WAF 202 HTML challenge, it cools down and rotates to next token."""
    calls = {"create": 0, "limited": [], "picked": []}

    def fake_pick(exclude=None):
        calls["picked"].append(set(exclude or ()))
        return 2 if exclude and 1 in exclude else 1

    async def fake_create_chat(token):
        calls["create"] += 1
        if calls["create"] == 1:
            raise functions.UpstreamError(
                429,
                "AWS WAF challenge (HTTP 202 text/html) on chat_session/create: refreshing WAF cookie",
                retry_after=15,
            )
        return f"chat-{token}"

    def fake_send(chat_id, auth_token, message, parent, thinking=False, search=False, file_ids_=None):
        async def ok_gen():
            yield "recovered after waf challenge"

        return ok_gen()

    def fake_limited(tid, code=None, cooldown=None):
        calls["limited"].append((tid, code, cooldown))

    patches = [
        ("get_auth_token", lambda: "tok"),
        ("generate_signature", lambda *a, **k: asyncio.sleep(0, result="sig-waf-202")),
        ("find_session", lambda s: None),
        ("pick_token", fake_pick),
        ("get_token", lambda tid: {"id": tid, "token": f"tok-{tid}", "status": "ACTIVE", "alias": f"t{tid}"}),
        ("send_message", fake_send),
        ("create_new_chat", fake_create_chat),
        ("extract_and_upload_files", lambda *a, **k: asyncio.sleep(0, result=[])),
        ("build_prompt", lambda *a, **k: asyncio.sleep(0, result="prompt")),
        ("mark_limited", fake_limited),
        ("mark_active", lambda tid: None),
        ("delete_sessions_for_chat", lambda *a: None),
        ("save_session", lambda *a, **k: None),
        ("parse_tools", lambda t: ([], t)),
        ("format_response", lambda text, model, messages, tools=None: text),
        ("needs_rollover", lambda messages, tools: False),
    ]

    async def run():
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await asyncio.wait_for(
                app_module.handle_chat([{"role": "user", "content": "hi"}], "test-model"),
                timeout=10,
            )
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    result = asyncio.run(run())
    assert result == "recovered after waf challenge"
    assert calls["create"] == 2
    assert calls["limited"] == [(1, 429, 15)]
    assert 1 in calls["picked"][-1]


def test_safe_json_waf_challenge_detection():
    """functions._safe_json handles 202 and ContentTypeError by invalidating cookies and raising 429."""
    import aiohttp
    import pytest

    class Fake202Resp:
        status = 202
        headers = {"Content-Type": "text/html; charset=utf-8"}

        async def json(self):
            raise aiohttp.ContentTypeError(None, None, status=202, message="Attempt to decode JSON with unexpected mimetype")

    class FakeContentTypeErrorResp:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}

        async def json(self):
            raise aiohttp.ContentTypeError(None, None, status=202, message="Attempt to decode JSON with unexpected mimetype")

    # 1. Status 202
    with pytest.raises(functions.UpstreamError) as exc_info:
        asyncio.run(functions._safe_json(Fake202Resp(), "test_context"))
    assert exc_info.value.status == 429
    assert "AWS WAF challenge" in str(exc_info.value)
    assert exc_info.value.retry_after == 15

    # 2. ContentTypeError on non-200 / 202
    with pytest.raises(functions.UpstreamError) as exc_info2:
        asyncio.run(functions._safe_json(FakeContentTypeErrorResp(), "test_context"))
    assert exc_info2.value.status == 429
    assert exc_info2.value.retry_after == 15
