"""Regression tests for the Stage 1 minor-fix batch.

* `minimal` is an explicit thinking-OFF effort value (OpenAI's gpt-5 default
  used to fall through by accident)
* /v1/responses accepts input_image parts (vision path already existed)
* empty/invalid JSON bodies answer 400, not 500
* the shared aiohttp session closes on lifespan shutdown

Run:  python tests/test_minor_fixes.py   (pytest-compatible)
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402


def test_minimal_effort_disables_thinking():
    assert app_module.is_thinking_enabled({"effort": "minimal"}) is False
    assert (
        app_module.is_thinking_enabled({"output_config": {"effort": "minimal"}})
        is False
    )
    assert app_module.is_thinking_enabled({"thinking": {"effort": "minimal"}}) is False
    assert app_module.is_thinking_enabled({"thinking": "minimal"}) is False
    assert app_module.is_thinking_enabled({"reasoning_effort": "minimal"}) is False


def test_responses_input_image_mapped():
    msgs = app_module._responses_input_to_messages(
        [
            "plain question",
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "what is this?"},
                    {"type": "input_image", "image_url": "https://example.com/x.png"},
                    {
                        "type": "input_image",
                        "image_url": {"url": "data:image/png;base64,AAAA"},
                    },
                    {"type": "input_image", "file_id": "file-img"},
                ],
            },
        ]
    )
    content = msgs[1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": "https://example.com/x.png"},
    }
    assert content[2] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    assert content[3] == {"type": "file", "file_id": "file-img"}
    assert msgs[0]["content"] == "plain question"


def test_responses_format_and_stream():
    import json
    import unittest.mock as mock

    chat_res = app_module.format_response(
        '<think>plan</think>Hi\n<tool_call name="lookup"><parameter name="q">x</parameter></tool_call>',
        "v4.1flash",
        [{"role": "user", "content": "hi"}],
    )
    resp = app_module.format_responses_response(
        chat_res,
        "v4.1flash",
        {
            "instructions": "sys",
            "tools": [{"type": "function", "name": "lookup", "parameters": {}}],
        },
    )
    assert resp["id"].startswith("resp_")
    assert resp["object"] == "response"
    assert resp["status"] == "completed"
    assert isinstance(resp["created_at"], int)
    assert resp["output"][0]["type"] == "reasoning"
    assert resp["output"][0]["summary"] == [{"type": "summary_text", "text": "plan"}]
    assert resp["output"][1]["type"] == "function_call"
    assert resp["output"][1]["name"] == "lookup"
    assert json.loads(resp["output"][1]["arguments"]) == {"q": "x"}
    assert "input_tokens" in resp["usage"]
    assert "output_tokens" in resp["usage"]

    async def _gen():
        yield "<think>step</think>"
        yield "Hello "
        yield "world"

    async def _collect():
        out = []
        async for frame in app_module.stream_responses_response(
            _gen(),
            "v4.1flash",
            [{"role": "user", "content": "hi"}],
            1,
            "sess",
            "sig",
            [],
        ):
            out.append(frame)
        return out

    with mock.patch.multiple(
        app_module,
        mark_active=mock.DEFAULT,
        save_session=mock.DEFAULT,
        generate_signature_sync=mock.DEFAULT,
        delete_sessions_for_chat=mock.DEFAULT,
    ):
        frames = asyncio.run(_collect())

    events = []
    for frame in frames:
        lines = [ln for ln in frame.strip().split("\n") if ln]
        assert lines[0].startswith("event: ")
        assert lines[1].startswith("data: ")
        evt_name = lines[0][len("event: ") :]
        data = json.loads(lines[1][len("data: ") :])
        assert data["type"] == evt_name
        events.append(data)

    assert [e["sequence_number"] for e in events] == list(range(len(events)))
    assert events[0]["type"] == "response.created"
    assert events[1]["type"] == "response.in_progress"
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["status"] == "completed"
    assert events[-1]["response"]["output"][0]["type"] == "reasoning"
    assert events[-1]["response"]["output"][1]["type"] == "message"
    assert events[-1]["response"]["output"][1]["content"][0] == {
        "type": "output_text",
        "text": "Hello world",
        "annotations": [],
        "logprobs": [],
    }

    sessions_store = {}
    sent_calls = []
    replies = [
        '<think>plan</think>Checking\n<tool_call name="shell"><parameter name="cmd">ls</parameter></tool_call>',
        "Found file1.txt",
        "Done!",
        "Fresh chat reply!",
    ]

    async def fake_create_chat(tok):
        return f"chat_{len(sent_calls)}"

    async def fake_send(
        chat_id,
        auth_token,
        prompt,
        parent_id,
        thinking=False,
        search=False,
        file_ids=None,
    ):
        reply = replies[len(sent_calls)]
        sent_calls.append((chat_id, parent_id, prompt))
        yield reply

    from fastapi.testclient import TestClient

    with mock.patch.multiple(
        app_module,
        check_key=lambda req: True,
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
        find_session=lambda s: sessions_store.get(s),
        save_session=lambda s, tid, sid, pid=0: sessions_store.__setitem__(
            s, {"token_id": tid, "session_id": sid, "parent_message_id": pid}
        ),
        delete_sessions_for_chat=lambda tid, sid: None,
    ):
        client = TestClient(app_module.app)
        t1_input = [
            {"role": "user", "content": [{"type": "input_text", "text": "run ls"}]},
        ]
        r1 = client.post(
            "/v1/responses",
            json={"model": "v4.1flash", "instructions": "sys", "input": t1_input},
        ).json()
        t2_input = (
            t1_input
            + r1["output"]
            + [
                {
                    "type": "function_call_output",
                    "call_id": r1["output"][-1]["call_id"],
                    "output": "file1.txt",
                }
            ]
        )
        r2 = client.post(
            "/v1/responses",
            json={"model": "v4.1flash", "instructions": "sys", "input": t2_input},
        ).json()
        r3 = client.post(
            "/v1/responses",
            json={
                "model": "v4.1flash",
                "previous_response_id": r2["id"],
                "input": "great, thanks",
            },
        ).json()
        r4 = client.post(
            "/v1/responses",
            json={"model": "v4.1flash", "instructions": "sys", "input": t1_input},
        ).json()
        assert r3["status"] == "completed"
        assert r4["status"] == "completed"
        assert sent_calls[0][0] == sent_calls[1][0] == sent_calls[2][0] == "chat_0"
        assert [c[1] for c in sent_calls[:3]] == [0, 2, 4]
        assert "Tool: shell" in sent_calls[1][2]
        assert sent_calls[3][0] == "chat_3"
        assert sent_calls[3][1] == 0


def test_invalid_json_returns_400():
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    saved = app_module.check_key
    app_module.check_key = lambda request: True
    try:
        r = client.post(
            "/v1/chat/completions",
            content=b"{definitely not json",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, (r.status_code, r.text)
        r = client.post(
            "/v1/chat/completions",
            content=b"",
            headers={"content-type": "application/json"},
        )
        assert r.status_code == 400, (r.status_code, r.text)
        r = client.post(
            "/v1/messages", content=b"[]", headers={"content-type": "application/json"}
        )
        assert r.status_code == 400, (r.status_code, r.text)
    finally:
        app_module.check_key = saved


def test_close_session_idempotent_and_resets():
    async def run():
        # No session open: must be a harmless no-op.
        await functions.close_session()
        assert functions._session is None
        # With an open session: closes and resets the handle.
        session = await functions.get_session()
        await functions.close_session()
        assert session.closed
        assert functions._session is None

    asyncio.run(run())


def main():
    tests = [
        v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)
    ]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
