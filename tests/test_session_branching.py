"""Two requests with the same history branch at the same upstream message.

Seen live: an agent (Hermes) forks a background review from the main thread.
Both requests share the history up to the last reply. Every completed turn
used to re-point that history's signature at the chat's end, so the second
branch was appended BEHIND the first branch's exchange and the model answered
as if it were still in the review. DeepSeek branches when given the fork point
as parent_message_id; reply ids are chat-global and come from the stream.
"""

import asyncio

import app as app_module
import functions


def test_same_history_forks_at_its_own_parent():
    sent = []
    store = {}
    chat = {"last_id": 2}

    async def scenario():
        app_module._chat_locks.clear()
        u1 = {"role": "user", "content": "u1"}
        a1 = {"role": "assistant", "content": "a1"}
        store[await app_module.generate_signature([u1, a1], "m")] = {
            "token_id": "t1", "session_id": "chat-1", "parent_message_id": 2,
        }

        def fake_send(chat_id, auth_token, message, parent, *args, **kw):
            sent.append(parent)
            chat["last_id"] += 2  # DeepSeek ids are chat-global
            functions.reply_id.set(chat["last_id"])  # what send_message reads from the SSE
            n = len(sent)

            async def gen():
                yield f"reply-{n}"

            return gen()

        async def fake_files(messages, token, last_user_only=False):
            return []

        async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
            return "prompt"

        def fake_save(sig, tid, sid, parent):
            store[sig] = {"token_id": tid, "session_id": sid, "parent_message_id": parent}

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("find_session", lambda s: dict(store[s]) if s in store else None),
            ("get_token", lambda tid: {"token": "tok", "status": "ACTIVE"}),
            ("send_message", fake_send),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda tid, sid: None),
            ("save_session", fake_save),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            async def ask(*msgs):
                return await asyncio.wait_for(app_module.handle_chat(list(msgs), "m"), 10)

            r1 = await ask(u1, a1, {"role": "user", "content": "main"})
            r2 = await ask(u1, a1, {"role": "user", "content": "review"})  # fork
            await ask(u1, a1, {"role": "user", "content": "review"},
                      {"role": "assistant", "content": r2}, {"role": "user", "content": "review 2"})
            await ask(u1, a1, {"role": "user", "content": "main"},
                      {"role": "assistant", "content": r1}, {"role": "user", "content": "main 2"})
        finally:
            for name, fn in saved:
                setattr(app_module, name, fn)

    asyncio.run(scenario())
    # main at 2 (ids 3,4); fork ALSO at 2 (ids 5,6); each branch continues
    # after its own reply: review at 6, main at 4.
    assert sent == [2, 2, 6, 4]
