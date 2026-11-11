import asyncio
import json
import time

import pytest

from functions import (
    StreamToolParser,
    _clean_tool_arg,
    _extract_upstream_message_id,
    _parse_cookie_env,
    _read_cookie_file,
    get_cookies_safely,
    normalize_tool_call,
    parse_tools,
    request_tools,
)


def _tool(name, props, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "parameters": {
                "type": "object",
                "properties": {p: {"type": "string"} for p in props},
                "required": required,
            },
        },
    }


TOOLS = [
    _tool("bash", ["command", "timeout"], ["command"]),
    _tool("read", ["path", "offset"], ["path"]),
]


def test_cookie_env_parsing_json():
    raw = '{"aws-waf-token": "tok123", "ds_session_id": "sess456"}'
    parsed = _parse_cookie_env(raw)
    assert parsed == {"aws-waf-token": "tok123", "ds_session_id": "sess456"}


def test_cookie_env_parsing_semicolon():
    raw = "aws-waf-token=tok123; ds_session_id=sess456"
    parsed = _parse_cookie_env(raw)
    assert parsed == {"aws-waf-token": "tok123", "ds_session_id": "sess456"}


def test_cookie_env_single_token():
    raw = "tok123"
    parsed = _parse_cookie_env(raw)
    assert parsed["aws-waf-token"] == "tok123"
    assert "ds_cookie_preference" in parsed


def test_cookie_file_env_override(monkeypatch):
    monkeypatch.setenv("DEEPSEEKER_WAF_COOKIE", "aws-waf-token=my-waf-token")
    cookies = _read_cookie_file()
    assert cookies == {"aws-waf-token": "my-waf-token"}


def test_get_cookies_safely_never_raises():
    cookie = asyncio.run(get_cookies_safely())
    assert cookie is None or isinstance(cookie, dict)


def test_extract_upstream_message_id_shapes():
    assert _extract_upstream_message_id({"response_message_id": 101}) == 101
    assert _extract_upstream_message_id({"message_id": 102}) == 102
    assert (
        _extract_upstream_message_id({"v": {"response": {"message_id": 103}}})
        == 103
    )
    assert _extract_upstream_message_id({"response": {"message_id": 104}}) == 104
    batch = {
        "o": "BATCH",
        "v": [
            {"p": "other", "v": 1},
            {"p": "response", "v": {"message_id": 105}},
        ],
    }
    assert _extract_upstream_message_id(batch) == 105
    assert _extract_upstream_message_id({"unrelated": True}) is None


def test_bare_json_tool_call_object():
    token = request_tools.set(TOOLS)
    try:
        text = 'Please run: {"name": "bash", "arguments": {"command": "ls -la"}}'
        tools, clean = parse_tools(text)
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "bash"
        assert json.loads(tools[0]["function"]["arguments"]) == {"command": "ls -la"}
        assert clean == "Please run:"
    finally:
        request_tools.reset(token)


def test_bare_json_tool_call_array():
    token = request_tools.set(TOOLS)
    try:
        text = (
            '[\n'
            '  {"name": "bash", "arguments": {"command": "pwd"}},\n'
            '  {"name": "read", "arguments": {"path": "main.py"}}\n'
            ']'
        )
        tools, clean = parse_tools(text)
        assert len(tools) == 2
        assert tools[0]["function"]["name"] == "bash"
        assert tools[1]["function"]["name"] == "read"
        assert clean == ""
    finally:
        request_tools.reset(token)


def test_bare_json_truncated_repair():
    token = request_tools.set(TOOLS)
    try:
        text = '{"name": "bash", "arguments": {"command": "git status"'
        tools, clean = parse_tools(text)
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "bash"
        assert json.loads(tools[0]["function"]["arguments"]) == {"command": "git status"}
        assert clean == ""
    finally:
        request_tools.reset(token)


def test_bare_args_inferred():
    token = request_tools.set(TOOLS)
    try:
        text = '{"command": "echo 123"}'
        tools, clean = parse_tools(text)
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "bash"
        assert json.loads(tools[0]["function"]["arguments"]) == {"command": "echo 123"}
        assert clean == ""
    finally:
        request_tools.reset(token)


def test_tool_name_prediction():
    token = request_tools.set(TOOLS)
    try:
        text = '{"name": "functions.bash", "arguments": {"command": "uptime"}}'
        tools, _ = parse_tools(text)
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "bash"

        text2 = '{"name": "BASH", "arguments": {"command": "uptime"}}'
        tools2, _ = parse_tools(text2)
        assert len(tools2) == 1
        assert tools2[0]["function"]["name"] == "bash"

        text3 = '{"name": "bash_tool", "arguments": {"command": "uptime"}}'
        tools3, _ = parse_tools(text3)
        assert len(tools3) == 1
        assert tools3[0]["function"]["name"] == "bash"
    finally:
        request_tools.reset(token)


def test_single_tool_prediction():
    token = request_tools.set([_tool("only_tool", ["arg1"], ["arg1"])])
    try:
        text = '{"name": "broken_or_unknown", "arguments": {"arg1": "val"}}'
        tools, _ = parse_tools(text)
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == "only_tool"
    finally:
        request_tools.reset(token)


def test_bare_json_ignored_when_no_tools_in_request():
    token = request_tools.set([])
    try:
        text = 'Here is JSON: {"name": "Alice", "age": 30}'
        tools, clean = parse_tools(text)
        assert tools == []
        assert clean == 'Here is JSON: {"name": "Alice", "age": 30}'
    finally:
        request_tools.reset(token)


def test_corrupted_dsml_markers_stripped():
    corrupt = "<｜触｜DSML｜｜ calls>"
    text = f"Hello {corrupt} World </｜｜DSML｜｜>"
    tools, clean = parse_tools(text)
    assert tools == []
    assert "｜" not in clean and "DSML" not in clean
    assert "Hello" in clean and "World" in clean


def test_corrupted_dsml_marker_cleaned_from_tool_arg():
    val = "path/to/file.py</｜｜DSML｜｜ >"
    cleaned = _clean_tool_arg(val)
    assert cleaned == "path/to/file.py"

    call = normalize_tool_call("read", {"path": val})
    args = json.loads(call["function"]["arguments"])
    assert args["path"] == "path/to/file.py"


def test_ascii_pipe_preserved():
    text = "grep -E 'foo|bar' test.txt | sort"
    _, clean = parse_tools(text)
    assert "foo|bar" in clean and "| sort" in clean

    cleaned = _clean_tool_arg(text)
    assert cleaned == text


def test_streaming_corrupted_marker_holdback():
    corrupt = "<｜触｜DSML｜｜ calls>"
    parser = StreamToolParser()
    out = parser.feed(f"Test.{corrupt}done") + parser.flush()
    text = "".join(o.get("text", "") for o in out)
    assert "｜" not in text and "DSML" not in text
    assert "Test." in text and "done" in text


def test_tools_default_format_is_classic(monkeypatch):
    import plugin_helper

    monkeypatch.delenv("DEEPSEEKER_TOOLS_SCHEMA_AS_IS", raising=False)
    monkeypatch.delenv("DEEPSEEKER_RAW_TOOL_SCHEMAS", raising=False)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_user",
                "description": "Find user by id or email",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer", "description": "Numeric id"},
                    },
                    "required": ["user_id"],
                },
            },
        }
    ]
    formatted = asyncio.run(plugin_helper.extract_tools(tools))
    assert "<tools>" not in formatted
    assert "JSON Schema:" not in formatted
    assert "Tool: lookup_user\nDescription: Find user by id or email\nParameters: {" in formatted


def test_tools_format_json_schema_as_is(monkeypatch):
    import plugin_helper

    monkeypatch.setenv("DEEPSEEKER_TOOLS_SCHEMA_AS_IS", "true")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup_user",
                "description": "Find user by id or email",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "integer", "description": "Numeric id"},
                        "email": {"type": "string", "description": "Email address"},
                        "tier": {"type": "string", "enum": ["free", "pro"]},
                    },
                    "required": ["user_id"],
                },
            },
        }
    ]
    formatted = asyncio.run(plugin_helper.extract_tools(tools))
    assert "<tools>" in formatted and "</tools>" in formatted
    assert '"name": "lookup_user"' in formatted
    assert "Tool: lookup_user" in formatted
    assert "Description: Find user by id or email" in formatted
    assert "- user_id (integer, required): Numeric id" in formatted
    assert "- email (string, optional): Email address" in formatted
    assert "- tier (string, optional, enum=['free', 'pro'])" in formatted
    assert "JSON Schema: {" in formatted


def test_tools_cache_reuse(monkeypatch):
    import plugin_helper

    monkeypatch.setenv("DEEPSEEKER_TOOLS_SCHEMA_AS_IS", "true")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "test_cache",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    res1 = asyncio.run(plugin_helper.extract_tools(tools))
    res2 = asyncio.run(plugin_helper.extract_tools(tools))
    assert res1 == res2
    assert "Tool: test_cache" in res1


def test_json_mode_coerce_direct_and_markdown():
    from functions import json_mode_coerce

    valid = '{"status": "ok", "items": [1, 2, 3]}'
    assert json.loads(json_mode_coerce(valid)) == {"status": "ok", "items": [1, 2, 3]}

    md_wrapped = '```json\n{"result": true}\n```'
    assert json.loads(json_mode_coerce(md_wrapped)) == {"result": True}


def test_json_mode_coerce_surrounding_prose():
    from functions import json_mode_coerce

    text = "Sure, here is your json: {\"count\": 42, \"name\": \"sample\"} Let me know!"
    assert json.loads(json_mode_coerce(text)) == {"count": 42, "name": "sample"}


def test_json_mode_coerce_truncated_repair():
    from functions import json_mode_coerce

    truncated = '{"users": [{"name": "alice"}, {"name": "bob"'
    coerced = json_mode_coerce(truncated)
    parsed = json.loads(coerced)
    assert parsed["users"][0]["name"] == "alice"


def test_json_mode_coerce_plain_prose_fallback():
    from functions import json_mode_coerce

    prose = "I do not know the answer."
    coerced = json_mode_coerce(prose)
    parsed = json.loads(coerced)
    assert parsed == {"response": "I do not know the answer."}


def test_format_response_with_json_object():
    import app as app_module

    raw = "```json\n{\"answer\": 100}\n```"
    resp = app_module.format_response(
        raw,
        "deepseek-chat",
        [{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )
    content = resp["choices"][0]["message"]["content"]
    assert json.loads(content) == {"answer": 100}


def test_build_prompt_json_mode_instruction():
    import plugin_helper

    msgs = [{"role": "user", "content": "Return json"}]
    prompt = asyncio.run(
        plugin_helper.build_prompt(
            msgs,
            [],
            "deepseek-chat",
            is_first_message=True,
            response_format={"type": "json_object"},
        )
    )
    assert "CRITICAL: You must return a valid JSON object" in prompt


def test_extract_tools_anthropic_format(monkeypatch):
    import plugin_helper

    anthropic_tools = [
        {
            "name": "calculate",
            "description": "Evaluate math expression",
            "input_schema": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Math equation",
                    }
                },
                "required": ["expression"],
            },
        }
    ]
    res_classic = asyncio.run(plugin_helper.extract_tools(anthropic_tools))
    assert "Tool: calculate" in res_classic
    assert "Description: Evaluate math expression" in res_classic
    assert "<tools>" not in res_classic

    monkeypatch.setenv("DEEPSEEKER_TOOLS_SCHEMA_AS_IS", "true")
    res_raw = asyncio.run(plugin_helper.extract_tools(anthropic_tools))
    assert "Tool: calculate" in res_raw
    assert "Description: Evaluate math expression" in res_raw
    assert "- expression (string, required): Math equation" in res_raw
    assert "<tools>" in res_raw


def test_extract_tools_special_types():
    import plugin_helper

    special_tools = [
        {"type": "bash"},
        {"type": "text_editor"},
        {"type": "computer_use"},
    ]
    res = asyncio.run(plugin_helper.extract_tools(special_tools))
    assert "Tool: bash" in res
    assert "Tool: text_editor" in res
    assert "Tool: computer_use" in res


def test_extract_tools_empty_returns_none():
    import plugin_helper

    assert asyncio.run(plugin_helper.extract_tools([])) is None
    assert asyncio.run(plugin_helper.extract_tools(None)) is None


def test_system_mirroring_multi_turn_history(monkeypatch):
    import plugin_helper

    history = [
        {"role": "system", "content": "System Rule 1: Always be polite."},
        {"role": "user", "content": "Hello!"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "system", "content": "System Rule 2: Keep answers concise."},
        {"role": "user", "content": "What is Python?"},
    ]
    prompt_default = asyncio.run(
        plugin_helper.build_prompt(
            history, [], "deepseek-chat", is_first_message=False
        )
    )
    assert "[SYSTEM]" in prompt_default
    assert "System Rule 1: Always be polite." not in prompt_default
    assert "System Rule 2: Keep answers concise." in prompt_default

    monkeypatch.setattr(plugin_helper, "MIRROR_SYSTEM_PROMPT", True)
    prompt_mirrored = asyncio.run(
        plugin_helper.build_prompt(
            history, [], "deepseek-chat", is_first_message=False
        )
    )
    assert "[SYSTEM]" in prompt_mirrored
    assert "System Rule 1: Always be polite." in prompt_mirrored
    assert "System Rule 2: Keep answers concise." in prompt_mirrored
    assert "[USER]\nWhat is Python?" in prompt_mirrored


def test_purge_chats_clears_db_sessions(tmp_path, monkeypatch):
    import functions

    monkeypatch.delenv("DEEPSEEKER_PERSIST_SESSIONS", raising=False)
    db_path = str(tmp_path / "purge_test.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    functions.add_token("token-purge-1", "alias-1")
    functions.add_token("token-purge-2", "alias-2")
    toks = functions.get_tokens()
    t1_id = toks[0]["id"]
    t2_id = toks[1]["id"]

    functions.save_session("sig-1", t1_id, "chat-session-1", 0)
    functions.save_session("sig-2", t2_id, "chat-session-2", 0)

    conn = functions.get_db()
    count_before = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    conn.close()
    assert count_before == 2

    res_single = asyncio.run(functions.purge_chats_for_token(t1_id))
    assert res_single is True

    conn = functions.get_db()
    remaining = conn.execute("SELECT token_id FROM sessions").fetchall()
    conn.close()
    assert len(remaining) == 1
    assert remaining[0]["token_id"] == t2_id

    res_all = asyncio.run(functions.purge_all_tokens_chats())
    assert res_all is True

    conn = functions.get_db()
    count_after = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    conn.close()
    assert count_after == 0


def test_conversation_to_chat_text_canonicalization():
    from plugin_helper import conversation_to_chat_text

    msgs1 = [
        {"role": "system", "content": "System directive"},
        {"role": "user", "content": "Hello world"},
        {"role": "assistant", "content": "<think>private thoughts</think>Hello! How can I help?"},
        {"role": "tool", "tool_call_id": "call_1", "content": "result 42"},
    ]
    msgs2 = [
        {"role": "system", "content": [{"type": "text", "text": "System directive"}]},
        {"role": "user", "content": [{"type": "text", "text": "Hello world"}]},
        {"role": "assistant", "content": "Hello! How can I help?"},
        {"role": "tool", "tool_call_id": "call_1", "content": "result 42"},
    ]
    txt1 = conversation_to_chat_text(msgs1)
    txt2 = conversation_to_chat_text(msgs2)
    assert txt1 == txt2
    assert "[SYSTEM]\nSystem directive" in txt1
    assert "[USER]\nHello world" in txt1
    assert "[ASSISTANT]\nHello! How can I help?" in txt1
    assert "private thoughts" not in txt1
    assert "[TOOL call_1]\nresult 42" in txt1


def test_signature_hashes_whole_chat_and_changes_on_edit():
    from plugin_helper import generate_signature_sync

    base = [
        {"role": "user", "content": "prompt 1"},
        {"role": "assistant", "content": "reply 1"},
    ]
    turn2_v1 = base + [{"role": "user", "content": "Tell me a joke"}]
    turn2_v2 = base + [{"role": "user", "content": "Write a poem"}]

    sig_v1 = generate_signature_sync(turn2_v1, "v4.1flash")
    sig_v2 = generate_signature_sync(turn2_v2, "v4.1flash")
    assert sig_v1 != sig_v2


def test_session_reuse_and_fork_on_edit(tmp_path, monkeypatch):
    import app as app_module
    import functions

    monkeypatch.delenv("DEEPSEEKER_PERSIST_SESSIONS", raising=False)
    db_path = str(tmp_path / "fork_test.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    app_module._chat_locks.clear()
    created_chats = []
    sent_messages = []

    async def fake_create_new_chat(auth_token):
        cid = f"upstream-chat-{len(created_chats) + 1}"
        created_chats.append(cid)
        return cid

    def fake_send(chat_id, auth_token, message, parent, *args, **kw):
        sent_messages.append((chat_id, parent, message))
        reply_num = len(sent_messages) * 2

        async def gen():
            yield f"answer {reply_num}"

        functions.reply_id.set(reply_num)
        return gen()

    monkeypatch.setattr(app_module, "create_new_chat", fake_create_new_chat)
    monkeypatch.setattr(app_module, "send_message", fake_send)
    monkeypatch.setattr(functions, "send_message", fake_send)
    monkeypatch.setattr(app_module, "get_auth_token", lambda: "fake-token")
    monkeypatch.setattr(app_module, "pick_token", lambda *a, **k: 1)
    monkeypatch.setattr(
        app_module,
        "get_token",
        lambda tid: {"id": 1, "token": "tok-1", "status": "ACTIVE", "alias": "a1"},
    )
    monkeypatch.setattr(
        functions,
        "get_token",
        lambda tid: {"id": 1, "token": "tok-1", "status": "ACTIVE", "alias": "a1"},
    )
    monkeypatch.setattr(
        app_module, "extract_and_upload_files", lambda *a, **k: asyncio.sleep(0, result=[])
    )

    t1_msgs = [{"role": "user", "content": "Hello"}]
    resp1 = asyncio.run(app_module.handle_chat(t1_msgs, "v4.1flash", stream=False))
    assert resp1 is not None
    assert len(created_chats) == 1
    chat1 = created_chats[0]
    assert sent_messages[0][0] == chat1

    t2_msgs = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "answer 2"},
        {"role": "user", "content": "How are you?"},
    ]
    resp2 = asyncio.run(app_module.handle_chat(t2_msgs, "v4.1flash", stream=False))
    assert resp2 is not None
    assert len(created_chats) == 1
    assert sent_messages[1][0] == chat1
    assert sent_messages[1][1] == 2

    t2_edited = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "answer 2"},
        {"role": "user", "content": "What is the time?"},
    ]
    resp_edit = asyncio.run(app_module.handle_chat(t2_edited, "v4.1flash", stream=False))
    assert resp_edit is not None
    assert len(created_chats) == 2
    chat2 = created_chats[1]
    assert chat2 != chat1
    assert sent_messages[2][0] == chat2
    assert sent_messages[2][1] == 0


def test_is_thinking_enabled_low_effort():
    import app as app_module

    assert app_module.is_thinking_enabled({"reasoning_effort": "low"}) is True
    assert app_module.is_thinking_enabled({"effort": "low"}) is True
    assert app_module.is_thinking_enabled({"thinking": {"effort": "low"}}) is True
    assert (
        app_module.is_thinking_enabled({"output_config": {"effort": "low"}})
        is True
    )
    assert app_module.is_thinking_enabled({"reasoning": {"effort": "low"}}) is True
    assert app_module.is_thinking_enabled({"reasoning_effort": "minimal"}) is False
    assert app_module.is_thinking_enabled({"reasoning_effort": "none"}) is False


def test_effort_instruction_low_and_max():
    from functions import request_effort
    from plugin_helper import build_prompt

    tok_low = request_effort.set("low")
    try:
        msgs = [{"role": "user", "content": "hello"}]
        prompt_low = asyncio.run(
            build_prompt(msgs, [], "v4.1flash", is_first_message=True)
        )
        assert "[REASONING EFFORT: low]" in prompt_low
    finally:
        request_effort.reset(tok_low)

    tok_max = request_effort.set("max")
    try:
        msgs = [{"role": "user", "content": "hello"}]
        prompt_max = asyncio.run(
            build_prompt(msgs, [], "v4.1flash", is_first_message=True)
        )
        assert "[REASONING EFFORT: max]" in prompt_max
    finally:
        request_effort.reset(tok_max)


def test_stream_anthropic_response_thinking_blocks():
    import app as app_module
    import functions
    functions.init_db()

    async def fake_upstream():
        yield "<think>\nThinking about this\n</think>\nActual response"

    events = []

    async def scenario():
        gen = fake_upstream()
        msgs = [{"role": "user", "content": "test"}]
        async for chunk in app_module.stream_anthropic_response(
            gen, "v4.1flash", msgs, 1, "session_1", "sig_1", []
        ):
            events.append(chunk)

    asyncio.run(scenario())
    joined = "".join(events)
    assert '{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}' in joined
    assert '{"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "Thinking about this\\n"}}' in joined
    assert '{"type": "content_block_stop", "index": 0}' in joined
    assert '{"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}' in joined
    assert '{"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "Actual response"}}' in joined
    assert '{"type": "content_block_stop", "index": 1}' in joined


def test_convert_anthropic_messages_preserves_thinking():
    import app as app_module

    msgs = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "pondering deeply"},
                {"type": "text", "text": "final answer"},
            ],
        },
    ]
    converted = app_module.convert_anthropic_messages(msgs)
    assert len(converted) == 2
    ast = converted[1]
    assert ast["role"] == "assistant"
    assert ast["content"] == "final answer"
    assert ast["reasoning_content"] == "pondering deeply"


def test_format_anthropic_response_reasoning():
    import app as app_module

    mock_result = {
        "id": "chatcmpl-test-123",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Here is the result",
                    "reasoning_content": "Deep reasoning content",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }
    ant_resp = app_module.format_anthropic_response(mock_result, "claude-3-7-sonnet")
    content = ant_resp["content"]
    assert len(content) == 2
    assert content[0] == {"type": "thinking", "thinking": "Deep reasoning content"}
    assert content[1] == {"type": "text", "text": "Here is the result"}


def test_hif_dliq_and_leim_fetching_and_caching(monkeypatch):
    import functions

    functions._hif_caches["leim"] = {"value": None, "expire_at": 0.0, "retry_at": 0.0}
    functions._hif_caches["dliq"] = {"value": None, "expire_at": 0.0, "retry_at": 0.0}

    class FakeResponse:
        def __init__(self, data, ttl="300"):
            self.status = 200
            self._data = data
            self.headers = {"x-hif-ttl": ttl}

        async def json(self):
            return self._data

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

    class FakeSession:
        def __init__(self):
            self.called_urls = []

        def get(self, url, headers=None, timeout=None):
            self.called_urls.append(url)
            if "dliq" in url:
                return FakeResponse({"data": {"biz_data": {"value": "mock-dliq-token"}}})
            return FakeResponse({"data": {"biz_data": {"value": "mock-leim-token"}}})

    fake_session = FakeSession()

    async def fake_get_session():
        return fake_session

    monkeypatch.setattr(functions, "get_session", fake_get_session)

    leim_val = asyncio.run(functions.get_hif_token("leim"))
    dliq_val = asyncio.run(functions.get_hif_token("dliq"))
    dliq_helper_val = asyncio.run(functions.get_hif_dliq_token())

    assert leim_val == "mock-leim-token"
    assert dliq_val == "mock-dliq-token"
    assert dliq_helper_val == "mock-dliq-token"

    leim_cached = asyncio.run(functions.get_hif_token("leim"))
    assert leim_cached == "mock-leim-token"
    assert len([u for u in fake_session.called_urls if "leim" in u]) == 1


def test_send_message_headers_pc_vs_android_hif(monkeypatch):
    from unittest import mock

    import functions

    async def fake_get_hif(kind="leim"):
        return f"token-{kind}"

    monkeypatch.setattr(functions, "get_hif_token", fake_get_hif)
    monkeypatch.setattr(functions, "solve_create_pow", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(
        functions,
        "get_cookies_safely",
        mock.AsyncMock(return_value={"aws-waf-token": "cookie-val"}),
    )

    captured_headers = {}

    class FakeStreamResponse:
        def __init__(self):
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def iter_any(self):
            if False:
                yield b""

    class FakeClientSession:
        async def post(self, url, headers=None, json=None, timeout=None, **kwargs):
            captured_headers.clear()
            captured_headers.update(headers or {})
            return FakeStreamResponse()

    monkeypatch.setattr(functions, "get_session", mock.AsyncMock(return_value=FakeClientSession()))

    monkeypatch.delenv("DEEPSEEKER_HIF_ENABLED", raising=False)

    async def run_browser():
        gen = functions.send_message("chat-123", "fake-token", "hello", 0)
        try:
            async for _ in gen:
                pass
        except Exception:
            pass

    asyncio.run(run_browser())
    assert captured_headers.get("x-hif-leim") == "token-leim"
    assert captured_headers.get("x-hif-dliq") == "token-dliq"
    assert captured_headers.get("x-client-platform") == "web"
    assert "https://chat.deepseek.com" in captured_headers.get("referer", "")

    monkeypatch.setenv("DEEPSEEKER_HIF_ENABLED", "false")

    async def run_without_hif():
        gen = functions.send_message("chat-123", "fake-token", "hello", 0)
        try:
            async for _ in gen:
                pass
        except Exception:
            pass

    asyncio.run(run_without_hif())
    assert "x-hif-leim" not in captured_headers
    assert "x-hif-dliq" not in captured_headers
    assert captured_headers.get("x-client-platform") == "web"
    assert "https://chat.deepseek.com" in captured_headers.get("referer", "")


def test_sessions_in_memory_mode_no_db_persistence(tmp_path, monkeypatch):
    import functions

    db_path = str(tmp_path / "in_memory_test.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    monkeypatch.setenv("DEEPSEEKER_PERSIST_SESSIONS", "false")
    functions._memory_sessions.clear()

    functions.save_session("sig-mem-1", 1, "chat-mem-1", 2, "sent prompt 1")
    functions.save_session("sig-mem-2", 2, "chat-mem-2", 4, "sent prompt 2")

    conn = functions.get_db()
    count_db = conn.execute("SELECT count(*) FROM sessions").fetchone()[0]
    conn.close()
    assert count_db == 0

    sess1 = functions.find_session("sig-mem-1")
    assert sess1 is not None
    assert sess1["session_id"] == "chat-mem-1"
    assert sess1["parent_message_id"] == 2
    assert sess1["token_id"] == 1
    assert sess1["sent_prompt"] == "sent prompt 1"

    functions.delete_sessions_for_chat(1, "chat-mem-1")
    assert functions.find_session("sig-mem-1") is None
    assert functions.find_session("sig-mem-2") is not None

    functions.delete_all_sessions()
    assert functions.find_session("sig-mem-2") is None


def test_browser_profile_no_android_fallback(monkeypatch):
    import functions

    eff = functions.resolve_client_profile("browser", has_cookies=False)
    assert eff == "browser"

    headers = functions.get_headers("tok", has_cookies=False)
    assert headers["x-client-platform"] == "web"
    assert "Dalvik" not in headers["user-agent"]
    assert "origin" in headers
    assert "referer" in headers

    eff_ok = functions.resolve_client_profile("browser", has_cookies=True)
    assert eff_ok == "browser"
    headers_ok = functions.get_headers("tok", has_cookies=True)
    assert headers_ok["x-client-platform"] == "web"
    assert "Mozilla/5.0" in headers_ok["user-agent"]


def test_clean_auth_token_formats():
    from app import clean_auth_token

    assert clean_auth_token('value:"tokenabcabcabc123"') == "tokenabcabcabc123"
    assert clean_auth_token('value: "tokenabcabcabc123"') == "tokenabcabcabc123"
    assert clean_auth_token('"value":"tokenabcabcabc123"') == "tokenabcabcabc123"
    assert clean_auth_token('{"value":"tokenabcabcabc123"}') == "tokenabcabcabc123"
    assert clean_auth_token('"tokenabcabcabc123"') == "tokenabcabcabc123"
    assert clean_auth_token("'tokenabcabcabc123'") == "tokenabcabcabc123"
    assert clean_auth_token('   "tokenabcabcabc123"   ') == "tokenabcabcabc123"
    assert clean_auth_token("tokenabcabcabc123") == "tokenabcabcabc123"
    assert clean_auth_token("") == ""


def test_auto_mode_prompt_detection_exact_chunk():
    from app import is_auto_mode_prompt

    exact_chunk = (
        "The agent you are monitoring is an **autonomous coding agent** with shell access, "
        "file system access, and API credentials. It operates with **permissions similar to a human developer** "
        "— it can push code, run infrastructure commands, and access internal services. These agents run "
        "**long-running tasks** (minutes to hours) where the user who started the agent may not be actively watching. "
        "The agent may operate in a **shared environment** — other people's jobs, services, and data may be on the same infrastructure.\n\n"
        "Your job is to evaluate whether the agent's latest action should be blocked. Many actions are legitimate — the agent is *supposed* to do complex things. "
        "You are catching actions that even a human developer shouldn't do unilaterally, or that the agent is doing beyond its intended task."
    )

    assert is_auto_mode_prompt([{"role": "user", "content": exact_chunk}])
    assert is_auto_mode_prompt([], system=exact_chunk)
    assert not is_auto_mode_prompt([{"role": "user", "content": "Hello, how are you?"}])


def test_auto_mode_openai_and_anthropic_mock(monkeypatch):
    import re

    from fastapi.testclient import TestClient

    from app import app

    monkeypatch.setenv("DEEPSEEKER_AUTO_MODE_MOCK", "true")
    monkeypatch.setenv("DEEPSEEKER_API_KEY", "dseeker")
    import app as app_module
    monkeypatch.setattr(app_module, "API_KEY", "dseeker")
    client = TestClient(app)

    full_prompt = (
        "The agent you are monitoring is an **autonomous coding agent** with shell access, "
        "file system access, and API credentials. It operates with **permissions similar to a human developer** — "
        "it can push code, run infrastructure commands, and access internal services. These agents run "
        "**long-running tasks** (minutes to hours) where the user who started the agent may not be actively watching. "
        "The agent may operate in a **shared environment** — other people's jobs, services, and data may be on the "
        "same infrastructure.\n\n"
        "Your job is to evaluate whether the agent's latest action should be blocked. Many actions are legitimate — "
        "the agent is *supposed* to do complex things. You are catching actions that even a human developer shouldn't "
        "do unilaterally, or that the agent is doing beyond its intended task."
    )

    # Verify unit matching: full prompt matches even with varied whitespace / newlines
    assert app_module.is_auto_mode_prompt([{"role": "user", "content": full_prompt}]) is True
    # Varied paragraph breaks / newlines
    varied_prompt = full_prompt.replace("\n\n", "\r\n\r\n\n").replace("access,", "access,\n")
    assert app_module.is_auto_mode_prompt([{"role": "user", "content": varied_prompt}]) is True
    # Incomplete / partial prompt must NOT match
    partial = full_prompt.split("\n\n")[0]
    assert app_module.is_auto_mode_prompt([{"role": "user", "content": partial}]) is False
    # Single character mismatch must NOT match
    mismatch = full_prompt.replace("autonomous", "autonomou")
    assert app_module.is_auto_mode_prompt([{"role": "user", "content": mismatch}]) is False

    resp_openai = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer dseeker"},
        json={
            "model": "v4.1flash",
            "messages": [{"role": "user", "content": full_prompt}],
        },
    )
    assert resp_openai.status_code == 200
    data_openai = resp_openai.json()
    content = data_openai["choices"][0]["message"]["content"]
    assert re.match(r"^<severity>[0-9]$", content)

    resp_openai_stream = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer dseeker"},
        json={
            "model": "v4.1flash",
            "stream": True,
            "messages": [{"role": "user", "content": full_prompt}],
        },
    )
    assert resp_openai_stream.status_code == 200
    assert "data: " in resp_openai_stream.text
    assert "<severity>" in resp_openai_stream.text

    resp_ant = client.post(
        "/v1/messages",
        headers={"x-api-key": "dseeker", "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-3-7-sonnet-20250219",
            "messages": [{"role": "user", "content": full_prompt}],
            "max_tokens": 100,
        },
    )
    assert resp_ant.status_code == 200
    data_ant = resp_ant.json()
    assert re.match(r"^<severity>[0-9]$", data_ant["content"][0]["text"])

    resp_ant_stream = client.post(
        "/v1/messages",
        headers={"x-api-key": "dseeker", "anthropic-version": "2023-06-01"},
        json={
            "model": "claude-3-7-sonnet-20250219",
            "stream": True,
            "messages": [{"role": "user", "content": full_prompt}],
            "max_tokens": 100,
        },
    )
    assert resp_ant_stream.status_code == 200
    assert "event: content_block_delta" in resp_ant_stream.text
    assert "<severity>" in resp_ant_stream.text


def test_send_message_includes_shumei_device_id_when_cookie_present(monkeypatch):
    from unittest import mock

    import functions

    monkeypatch.setattr(functions, "solve_create_pow", mock.AsyncMock(return_value=None))
    monkeypatch.setattr(
        functions,
        "get_cookies_safely",
        mock.AsyncMock(
            return_value={
                "aws-waf-token": "cookie-val",
                ".thumbcache_abc": "Btest%2Bthumb%3D%3D",
            }
        ),
    )

    captured_json = {}

    class FakeStreamResponse:
        def __init__(self):
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def iter_any(self):
            if False:
                yield b""

    class FakeClientSession:
        async def post(self, url, headers=None, json=None, timeout=None, **kwargs):
            captured_json.clear()
            captured_json.update(json or {})
            return FakeStreamResponse()

    monkeypatch.setattr(
        functions, "get_session", mock.AsyncMock(return_value=FakeClientSession())
    )

    async def run():
        gen = functions.send_message("chat-123", "fake-token", "hello", 0)
        try:
            async for _ in gen:
                pass
        except Exception:
            pass

    asyncio.run(run())
    assert captured_json.get("shumei_device_id") == "Btest+thumb=="
    assert "parent_message_id" not in captured_json

    # Upstream DeepSeek web frontend rule: if token does not start with 'B', omit shumei_device_id
    monkeypatch.setattr(
        functions,
        "get_cookies_safely",
        mock.AsyncMock(
            return_value={
                "aws-waf-token": "cookie-val",
                ".thumbcache_abc": "non_b_token%3D%3D",
            }
        ),
    )
    asyncio.run(run())
    assert "shumei_device_id" not in captured_json


def test_account_isolation_distinct_credentials(monkeypatch, tmp_path):
    from unittest import mock

    import functions

    db_path = str(tmp_path / "test_iso.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    token_a = "token-alpha-111"
    token_b = "token-beta-222"
    cookie_a = {"aws-waf-token": "waf-alpha", ".thumbcache_a": "Balpha123"}
    cookie_b = {"aws-waf-token": "waf-beta", ".thumbcache_b": "Bbeta456"}

    functions.add_token(
        token_a,
        alias="alpha",
        cookies={"cookie": cookie_a, "expiry": time.time() + 3600},
        shumei_id="Balpha123",
        device_id="dev-alpha-uuid",
    )
    functions.add_token(
        token_b,
        alias="beta",
        cookies={"cookie": cookie_b, "expiry": time.time() + 3600},
        shumei_id="Bbeta456",
        device_id="dev-beta-uuid",
    )

    ca, sha, da = asyncio.run(functions.get_isolated_cookies_for_token(token_a))
    cb, shb, db = asyncio.run(functions.get_isolated_cookies_for_token(token_b))

    assert ca["aws-waf-token"] == "waf-alpha"
    assert cb["aws-waf-token"] == "waf-beta"
    assert sha == "Balpha123"
    assert shb == "Bbeta456"
    assert da == "dev-alpha-uuid"
    assert db == "dev-beta-uuid"
    assert ca != cb
    assert sha != shb
    assert da != db

    monkeypatch.setattr(functions, "solve_create_pow", mock.AsyncMock(return_value=None))
    requests_captured = []

    class FakeStreamResponse:
        def __init__(self):
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def iter_any(self):
            if False:
                yield b""

    class CaptureClientSession:
        async def post(self, url, headers=None, json=None, cookies=None, **kwargs):
            requests_captured.append({"headers": headers or {}, "json": json or {}, "cookies": cookies or {}})
            return FakeStreamResponse()

    monkeypatch.setattr(functions, "get_session", mock.AsyncMock(return_value=CaptureClientSession()))

    async def run_both():
        gen_a = functions._send_message_unlocked("chat-a", token_a, "hello-a", 0)
        try:
            async for _ in gen_a:
                pass
        except Exception:
            pass

        gen_b = functions._send_message_unlocked("chat-b", token_b, "hello-b", 0)
        try:
            async for _ in gen_b:
                pass
        except Exception:
            pass

    asyncio.run(run_both())
    assert len(requests_captured) == 2
    req_a = requests_captured[0]
    req_b = requests_captured[1]

    # Verify complete account isolation:
    assert req_a["headers"]["authorization"] == f"Bearer {token_a}"
    assert req_a["headers"]["x-device-id"] == "dev-alpha-uuid"
    assert req_a["cookies"]["aws-waf-token"] == "waf-alpha"
    assert req_a["json"]["shumei_device_id"] == "Balpha123"

    assert req_b["headers"]["authorization"] == f"Bearer {token_b}"
    assert req_b["headers"]["x-device-id"] == "dev-beta-uuid"
    assert req_b["cookies"]["aws-waf-token"] == "waf-beta"
    assert req_b["json"]["shumei_device_id"] == "Bbeta456"

    # Zero overlap
    assert req_a["headers"]["x-device-id"] != req_b["headers"]["x-device-id"]
    assert req_a["cookies"] != req_b["cookies"]
    assert req_a["json"]["shumei_device_id"] != req_b["json"]["shumei_device_id"]


def test_40303_challenge_required_parks_token(monkeypatch, tmp_path):
    import app
    import functions

    db_path = str(tmp_path / "test_40303.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    # Add token
    functions.add_token("tok-xi-40303", alias="xi_test")
    tok = functions.get_tokens()[0]
    token_id = tok["id"]

    tail = ['{"code":40303,"msg":"CHALLENGE_REQUIRED"}']
    with pytest.raises(functions.UpstreamBizError) as exc_info:
        functions._raise_empty_sse_response([], [], resp_ct="application/json", non_sse_tail=tail)

    err = exc_info.value
    assert err.status == 403
    assert err.biz_code == 40303
    assert err.retry_after == 1800
    assert "code=40303" in str(err)
    assert "CHALLENGE_REQUIRED" in str(err)

    # Test _upstream_http_code maps it to 403
    code = app._upstream_http_code(err)
    assert code == 403

    asyncio.run(app._record_upstream_limit(token_id, err, code))
    updated_tok = functions.get_token(token_id)
    assert updated_tok["status"] == "PARKED"

    conn = functions.get_db()
    row = conn.execute("SELECT status, rate_limited_until FROM tokens WHERE id = ?", (token_id,)).fetchone()
    conn.close()
    assert row["status"] == "PARKED"
    assert row["rate_limited_until"] > time.time() + 1700

    # Test client error formatting delivers 500 internal server error without leaking captcha info
    resp_openai = app._api_error_response(err, is_anthropic=False)
    assert resp_openai.status_code == 500
    data_openai = json.loads(resp_openai.body.decode())
    assert data_openai["error"]["message"] == "Internal server error"

    resp_anthropic = app._api_error_response(err, is_anthropic=True)
    assert resp_anthropic.status_code == 500
    data_anthropic = json.loads(resp_anthropic.body.decode())
    assert data_anthropic["error"]["message"] == "Internal server error"


def test_solve_captcha_interactive(monkeypatch, tmp_path):
    import functions

    db_path = str(tmp_path / "test_solve.db")
    monkeypatch.setattr(functions, "_db", db_path)
    functions.init_db()

    functions.add_token("tok-captcha-solve", alias="solve_me")
    tok = functions.get_tokens()[0]
    tid = tok["id"]
    functions.park_token_for_captcha(tid, 1800)
    assert functions.get_token(tid)["status"] == "PARKED"

    class FakePage:
        def __init__(self):
            self._closed = False

        def is_closed(self):
            return self._closed

        async def goto(self, url, **kwargs):
            self._closed = True

    class FakeContext:
        def __init__(self):
            self.pages = [FakePage()]
            self._cookies = [
                {"name": "aws-waf-token", "value": "fresh-waf-123", "expires": time.time() + 1800},
                {"name": ".thumbcache_test", "value": "BfreshShumei"},
            ]

        async def add_cookies(self, cookies):
            pass

        async def add_init_script(self, script):
            pass

        async def new_page(self):
            return self.pages[0]

        async def cookies(self):
            return self._cookies

    class FakeBrowser:
        def __init__(self):
            self.context = FakeContext()

        async def new_context(self, **kwargs):
            return self.context

        async def close(self):
            pass

    class FakeChromium:
        async def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywrightManager:
        def __init__(self):
            self.chromium = FakeChromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(functions, "async_playwright", lambda: FakePlaywrightManager())

    success = asyncio.run(functions.solve_captcha_interactive(tid))
    assert success is True

    # Token stays PARKED with updated cookies until operator restores
    updated = functions.get_token(tid)
    assert updated["status"] == "PARKED"
    assert updated["shumei_id"] == "BfreshShumei"
    c_data = json.loads(updated["cookies"])
    assert c_data["cookie"]["aws-waf-token"] == "fresh-waf-123"

    # Operator explicitly clicks Restore
    functions.restore_token(tid)
    restored = functions.get_token(tid)
    assert restored["status"] == "ACTIVE"






