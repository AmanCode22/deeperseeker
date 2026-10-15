import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from plugin_helper import build_prompt, extract_system


def run(coro):
    return asyncio.run(coro)


def test_extract_system_single_message_unchanged():
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "hi"},
    ]
    assert run(extract_system(messages)) == "You are helpful."


def test_extract_system_concatenates_all_in_order():
    messages = [
        {"role": "system", "content": "A"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "B"},
    ]
    assert run(extract_system(messages)) == "A\n\nB"


def test_extract_system_flattens_list_content():
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "first"},
                {"type": "input_image", "image_url": "https://example.com/x.png"},
                {"type": "text", "text": "second"},
            ],
        }
    ]
    assert run(extract_system(messages)) == "first\nsecond"


def test_extract_system_ignores_empty_entries():
    assert run(extract_system([{"role": "user", "content": "hi"}])) is None
    messages = [
        {"role": "system", "content": ""},
        {"role": "system", "content": []},
        {"role": "system", "content": "real"},
        {"role": "system"},
    ]
    assert run(extract_system(messages)) == "real"


def test_build_prompt_first_message_includes_all_system_once():
    messages = [
        {"role": "system", "content": "S1"},
        {"role": "system", "content": "S2"},
        {"role": "user", "content": "hello"},
    ]
    prompt = run(build_prompt(messages, [], "v4.1flash", is_first_message=True))
    assert prompt.count("[SYSTEM]") == 1
    assert prompt.split("[SYSTEM]")[1].split("[USER]")[0].index("S1") < prompt.index(
        "S2"
    )
    assert prompt.count("S1") == 1
    assert prompt.count("S2") == 1
    assert "[PREVIOUS CONVERSATION HISTORY]" not in prompt


def test_build_prompt_continuation_forwards_trailing_system():
    messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ok"},
        {"role": "system", "content": "remember: be terse"},
        {"role": "user", "content": "next"},
    ]
    prompt = run(build_prompt(messages, [], "v4.1flash", is_first_message=False))
    assert "[SYSTEM]\nremember: be terse" in prompt
    assert prompt.index("remember: be terse") < prompt.index("[USER]\nnext")


def test_build_prompt_continuation_does_not_resend_earlier_system():
    messages = [
        {"role": "system", "content": "SENTINEL"},
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "next"},
    ]
    prompt = run(build_prompt(messages, [], "v4.1flash", is_first_message=False))
    assert "SENTINEL" not in prompt
    assert "[USER]\nnext" in prompt


def test_chat_completions_merges_system_messages_in_upstream_prompt():
    from fastapi.testclient import TestClient

    sent = []

    async def fake_create_chat(token):
        return "chat_1"

    async def fake_send(
        chat_id,
        auth_token,
        prompt,
        parent_id,
        thinking=False,
        search=False,
        file_ids=None,
    ):
        sent.append(prompt)
        yield "ok"

    payload = {
        "model": "v4.1flash",
        "messages": [
            {"role": "system", "content": "S1"},
            {"role": "system", "content": "S2"},
            {"role": "user", "content": "hello"},
        ],
    }

    with mock.patch.multiple(
        app_module,
        check_key=lambda request: True,
        get_auth_token=lambda: "tok",
        pick_token=lambda exclude=None: 1,
        get_token=lambda tid: {
            "id": 1,
            "token": "tok",
            "status": "ACTIVE",
            "alias": "t",
        },
        create_new_chat=fake_create_chat,
        send_message=fake_send,
        mark_active=lambda tid: None,
        find_session=lambda s: None,
        save_session=lambda s, tid, sid, pid=0: None,
        delete_sessions_for_chat=lambda tid, sid: None,
        generate_signature_sync=lambda messages, model, scope="": "sig",
    ):
        response = TestClient(app_module.app).post("/v1/chat/completions", json=payload)

    assert response.status_code == 200, response.text
    assert sent, "no upstream prompt was built"
    prompt = sent[0]
    assert prompt.count("[SYSTEM]") == 1
    assert "S1" in prompt and "S2" in prompt
    assert prompt.count("S1") == 1 and prompt.count("S2") == 1
    assert "[PREVIOUS CONVERSATION HISTORY]" not in prompt
    assert "[USER]\nhello" in prompt
