"""Regression tests for file ownership pinning (Stage 1 audit, B4).

/v1/files uploads picked a RANDOM token and upstream files are account-
scoped, so a chat referencing a file_id could land on a different token and
get "file not found" — the OpenAI-style upload->reference flow was broken by
design. Uploads are now pinned (files table), first-turn chats prefer the
file-owner token, and later turns re-home foreign references onto the chat's
own token.

Run:  python tests/test_file_ownership.py   (pytest-compatible)
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    functions._db = os.path.join(tmpdir, "files.db")
    if os.path.exists(functions._db):
        os.remove(functions._db)
    functions.init_db()


def test_record_and_lookup_first_owner_wins():
    _fresh_db()
    functions.record_file("file-1", 2)
    assert functions.get_file_token("file-1") == 2
    functions.record_file("file-1", 5)  # a later upload elsewhere must NOT steal ownership
    assert functions.get_file_token("file-1") == 2
    assert functions.get_file_token("missing") is None


def test_referenced_file_ids_scan():
    import app as app_module

    msgs = [
        {"role": "user", "content": "plain text"},
        {"role": "user", "content": [
            {"type": "text", "text": "use this"},
            {"type": "file", "file": {"file_id": "file-openai"}},
        ]},
        {"role": "user", "content": [
            {"type": "document", "source": {"type": "file", "file_id": "file-anthropic"}},
        ]},
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "data": "..."}},  # not a reference
        ]},
    ]
    assert app_module._referenced_file_ids(msgs) == ["file-openai", "file-anthropic"]
    assert app_module._referenced_file_ids([{"role": "user", "content": "str"}]) == []


def test_rehome_replaces_foreign_and_keeps_owned():
    import app as app_module

    _fresh_db()
    functions.record_file("file-owned", 1)
    functions.record_file("file-foreign", 2)
    # "file-legacy" predates the registry: no owner is known.

    async def fake_get_file_content(fetch_token, file_id):
        yield "text/plain"
        yield b"data"

    async def fake_upload(file_bytes, file_name, file_content_type, auth_token):
        assert auth_token == "tok-1", "the copy must be uploaded with the CHAT's token"
        yield ("uploaded", "ignored")
        yield ("success", {"file_id": "file-copy", "openai_timestamp": 0, "size": 4,
                           "anthropic_timestamp": "1970-01-01T00:00:00Z"})

    saved_get_token = app_module.get_token
    saved_get_file_content = app_module.get_file_content
    saved_upload = app_module.upload_file
    app_module.get_token = lambda tid: {"id": tid, "token": f"tok-{tid}", "status": "ACTIVE"}
    app_module.get_file_content = fake_get_file_content
    app_module.upload_file = fake_upload
    try:
        tok = {"id": 1, "token": "tok-1", "status": "ACTIVE"}
        out = asyncio.run(app_module._rehome_foreign_files(
            ["file-owned", "file-foreign", "file-legacy"], 1, tok
        ))
    finally:
        app_module.get_token = saved_get_token
        app_module.get_file_content = saved_get_file_content
        app_module.upload_file = saved_upload

    assert out == ["file-owned", "file-copy", "file-legacy"], out
    assert functions.get_file_token("file-copy") == 1, "the copy must be pinned to the chat's token"
    assert functions.get_file_token("file-foreign") == 2, "the original mapping must stay untouched"


def test_chat_prefers_file_owner_token_on_first_turn():
    import app as app_module

    calls = {"tokens": [], "sessions": []}

    async def scenario():
        app_module._chat_locks.clear()
        sig = "sig-file-owner"

        async def fake_sig(messages, model, scope=""):
            return sig

        def fake_pick(*a, **k):
            return 1  # the scheduler picks token 1...

        def fake_get_token(tid):
            return {"id": tid, "token": f"tok-{tid}", "status": "ACTIVE"}

        def fake_send(chat_id, auth_token, message, parent, thinking=False, search=False, file_ids_=None):
            calls["tokens"].append((auth_token, tuple(file_ids_ or [])))

            async def gen():
                yield "ok"
            return gen()

        async def fake_create_chat(token):
            return "chat-owned"

        async def fake_files(messages, token, last_user_only=False):
            return []  # nothing new to upload

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        def fake_save(s, tid, sid, parent):
            calls["sessions"].append((tid, sid, parent))

        messages = [{"role": "user", "content": [
            {"type": "text", "text": "what does this say?"},
            {"type": "file", "file": {"file_id": "file-openai"}},
        ]}]

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("get_file_token", lambda fid: 2),  # ...but token 2 owns the file
            ("send_message", fake_send),
            ("create_new_chat", fake_create_chat),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", fake_save),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await app_module.handle_chat(messages, "v4.1flash")
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    try:
        result = asyncio.run(scenario())
    finally:
        app_module._chat_locks.clear()
    assert result == "ok"
    assert calls["tokens"] == [("tok-2", ())], \
        f"the chat must run on the file-owner token, got {calls['tokens']}"
    assert calls["sessions"] and calls["sessions"][0][0] == 2, \
        "the session must be saved against the owner token"


TESTS = [
    test_record_and_lookup_first_owner_wins,
    test_referenced_file_ids_scan,
    test_rehome_replaces_foreign_and_keeps_owned,
    test_chat_prefers_file_owner_token_on_first_turn,
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
