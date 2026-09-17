import asyncio
import json
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions import parse_tools, normalize_tool_call, StreamToolParser
from plugin_helper import enrich_tool_names, extract_tool_results, build_prompt
from app import stream_response, stream_anthropic_response


def test_reject_pseudo_tool_names():
    """Generic wrapper names like 'tool_call', 'invoke', 'function_call' must be rejected
    when they do not contain a valid nested tool call."""
    assert normalize_tool_call("tool_call", {}) is None
    assert normalize_tool_call("invoke", {}) is None
    assert normalize_tool_call("function_call", {}) is None
    assert normalize_tool_call({"name": "tool_call", "arguments": {}}) is None
    assert normalize_tool_call({"name": "tool_call", "arguments": {"command": "ls"}}) is None


def test_unwrap_valid_nested_tool_call():
    """If a tool call is wrapped in a generic envelope, it should be unwrapped to the real tool."""
    raw = {
        "name": "tool_call",
        "arguments": {
            "name": "skill_view",
            "arguments": {"skill": "hermes-cron-and-routing"},
        },
    }
    norm = normalize_tool_call(raw)
    assert norm is not None
    assert norm["function"]["name"] == "skill_view"
    args = json.loads(norm["function"]["arguments"])
    assert args == {"skill": "hermes-cron-and-routing"}


def test_parse_tools_named_tag_with_json_body():
    """Model emitting <tool_call name="X">{"param": "val"}</tool_call> must extract arguments."""
    text = (
        '<tool_call name="skill_view">{"skill": "hermes-cron-and-routing"}</tool_call>'
        '<tool_call name="skill_view">{"skill": "web-search"}</tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    assert tools[0]["function"]["name"] == "skill_view"
    assert json.loads(tools[0]["function"]["arguments"]) == {"skill": "hermes-cron-and-routing"}
    assert tools[1]["function"]["name"] == "skill_view"
    assert json.loads(tools[1]["function"]["arguments"]) == {"skill": "web-search"}


def test_parse_tools_multiple_json_in_single_wrapper():
    """Model emitting multiple JSON calls in a single <tool_call> tag must parse all of them."""
    text = (
        '<tool_call>\n'
        '{"name": "skill_view", "arguments": {"skill": "hermes-cron-and-routing"}}\n'
        '{"name": "skill_view", "arguments": {"skill": "other-skill"}}\n'
        '</tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    assert json.loads(tools[0]["function"]["arguments"]) == {"skill": "hermes-cron-and-routing"}
    assert json.loads(tools[1]["function"]["arguments"]) == {"skill": "other-skill"}


def test_parse_tools_json_array():
    """Model emitting a JSON list of tool calls inside <tool_call> must parse all of them."""
    text = (
        '<tool_call>[\n'
        '  {"name": "skill_view", "arguments": {"skill": "hermes-cron-and-routing"}},\n'
        '  {"name": "skill_view", "arguments": {"skill": "other-skill"}}\n'
        ']</tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    assert json.loads(tools[0]["function"]["arguments"]) == {"skill": "hermes-cron-and-routing"}
    assert json.loads(tools[1]["function"]["arguments"]) == {"skill": "other-skill"}


def test_parse_tools_respects_closing_tags_and_prose():
    """Closing tags must truncate parameter parsing so trailing prose or adjacent tools do not leak."""
    text = (
        '<tool_call name="skill_view">\n'
        '<parameter name="skill">hermes-cron-and-routing</parameter>\n'
        '</tool_call>\n'
        '<content>Some prose here that should not become a parameter</content>\n'
        '<tool_call name="skill_view">\n'
        '<parameter name="skill">web-search</parameter>\n'
        '</tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    args0 = json.loads(tools[0]["function"]["arguments"])
    assert args0 == {"skill": "hermes-cron-and-routing"}
    assert "content" not in args0
    args1 = json.loads(tools[1]["function"]["arguments"])
    assert args1 == {"skill": "web-search"}


def test_enrich_tool_names_preserves_arguments():
    """enrich_tool_names must attach both name and arguments to role=tool messages."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "skill_view", "arguments": '{"skill": "hermes-cron-and-routing"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "skill_view", "arguments": '{"skill": "web-search"}'},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "Skill web-search not found"},
        {"role": "tool", "tool_call_id": "call_1", "content": "Hermes cron skill docs..."},
    ]
    enriched = enrich_tool_names(messages)
    assert enriched[1]["name"] == "skill_view"
    assert enriched[1]["tool_arguments"] == '{"skill": "web-search"}'
    assert enriched[2]["name"] == "skill_view"
    assert enriched[2]["tool_arguments"] == '{"skill": "hermes-cron-and-routing"}'


@pytest.mark.asyncio
async def test_extract_tool_results_reorders_and_includes_arguments():
    """extract_tool_results must reorder tool results to match assistant call order and include arguments,
    preventing crossed results when tools finish concurrently."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "skill_view", "arguments": '{"skill": "hermes-cron-and-routing"}'},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "skill_view", "arguments": '{"skill": "web-search"}'},
                },
            ],
        },
        # Client executed call_2 first and appended it first
        {"role": "tool", "tool_call_id": "call_2", "content": "Skill web-search not found. Available: ['hermes-cron-and-routing']"},
        {"role": "tool", "tool_call_id": "call_1", "content": "Hermes routing docs content..."},
    ]
    enriched = enrich_tool_names(messages)
    prompt_results = await extract_tool_results(enriched, latest_only=True)

    # Must be sorted matching call_1 then call_2
    lines = prompt_results.split("\n\n")
    assert len(lines) == 2
    assert 'Tool: skill_view({"skill": "hermes-cron-and-routing"}) (Call ID: call_1)' in lines[0]
    assert "Hermes routing docs content..." in lines[0]
    assert 'Tool: skill_view({"skill": "web-search"}) (Call ID: call_2)' in lines[1]
    assert "Skill web-search not found" in lines[1]


@pytest.mark.asyncio
async def test_stream_response_first_chunk_has_role_and_persists_effective_tools(monkeypatch):
    """stream_response must include role=assistant in first delta and persist effective_tools."""
    saved_turns = []

    def mock_save_session(sig, token_id, session_id, parent_id):
        saved_turns.append((sig, token_id, session_id, parent_id))

    monkeypatch.setattr("app.save_session", mock_save_session)
    monkeypatch.setattr("app.mark_active", lambda *a: None)

    async def sample_gen():
        yield '<tool_call name="skill_view">'
        yield '{"skill": "hermes-cron-and-routing"}'
        yield '</tool_call>'

    chunks = []
    gen = stream_response(sample_gen(), "v4.1flash", [{"role": "user", "content": "view skill"}], 1, "s1", "sig1", [])
    async for chunk in gen:
        chunks.append(chunk)

    # First data chunk must have role: assistant
    data_lines = [c for c in chunks if c.startswith("data: {")]
    first_json = json.loads(data_lines[0][6:].strip())
    assert first_json["choices"][0]["delta"].get("role") == "assistant"

    # Tool call chunk must be present
    tool_chunks = [c for c in data_lines if "tool_calls" in c]
    assert len(tool_chunks) >= 1
    assert "hermes-cron-and-routing" in tool_chunks[0]

    # SSE must end with [DONE]
    assert chunks[-1] == "data: [DONE]\n\n"


def test_write_file_parameter_aliasing_and_dsml_stripping():
    """write_file must map file_content -> content and file_path -> path,
    and strip DSML tokens (even when repeated) from the path."""
    # 1. file_content -> content
    raw = {
        "name": "write_file",
        "arguments": {
            "path": "/home/user/workspace/script.py",
            "file_content": "print('hello world')",
        },
    }
    norm = normalize_tool_call(raw)
    assert norm is not None
    args = json.loads(norm["function"]["arguments"])
    assert args["content"] == "print('hello world')"
    assert args["path"] == "/home/user/workspace/script.py"
    # The source key must be consumed, not left duplicated alongside the
    # canonical one — a live hermes-agent session reported write_file calls
    # failing schema validation (or silently doubling large payload size)
    # because the aliased-from key survived in the final args dict.
    assert "file_content" not in args

    # 2. file_path -> path and DSML token in path stripped
    raw2 = {
        "name": "write_file",
        "arguments": {
            "file_path": "/home/user/workspace/script.py</｜｜DSML｜｜>",
            "file_content": "data",
        },
    }
    norm2 = normalize_tool_call(raw2)
    assert norm2 is not None
    args2 = json.loads(norm2["function"]["arguments"])
    assert args2["path"] == "/home/user/workspace/script.py"
    assert args2["content"] == "data"
    assert "file_path" not in args2
    assert "file_content" not in args2

    # 3. Path with 1500x repetition of </｜｜DSML｜｜>
    repeated_dsml_path = "/home/user/workspace/test.txt" + ("</｜｜DSML｜｜>" * 1500)
    norm3 = normalize_tool_call("write_file", {"path": repeated_dsml_path, "content": "hello"})
    assert norm3 is not None
    args3 = json.loads(norm3["function"]["arguments"])
    assert args3["path"] == "/home/user/workspace/test.txt"

    # 4. An empty-string content is a deliberate "write an empty file" and
    # must NOT be aliased over by a candidate key (unlike path/code/command,
    # which treat "" as missing).
    raw4 = {"name": "write_file", "arguments": {"path": "/tmp/empty.txt", "content": ""}}
    norm4 = normalize_tool_call(raw4)
    args4 = json.loads(norm4["function"]["arguments"])
    assert args4["content"] == ""


def test_execute_code_and_bash_parameter_aliasing():
    """execute_code must alias command/script -> code, and bash must alias cmd -> command,
    defaulting to empty string instead of None to prevent NoneType errors. The source
    key must not survive alongside the canonical one (duplicated payload / extra-field
    schema rejection)."""
    # execute_code command -> code
    norm_code = normalize_tool_call("execute_code", {"command": "import sys; print(sys.version)"})
    assert norm_code is not None
    args_code = json.loads(norm_code["function"]["arguments"])
    assert args_code["code"] == "import sys; print(sys.version)"
    assert "command" not in args_code

    # execute_code with missing code defaults to ""
    norm_empty_code = normalize_tool_call("execute_code", {})
    assert norm_empty_code is not None
    args_empty = json.loads(norm_empty_code["function"]["arguments"])
    assert args_empty["code"] == ""

    # bash cmd -> command
    norm_bash = normalize_tool_call("bash", {"cmd": "pytest -v"})
    assert norm_bash is not None
    args_bash = json.loads(norm_bash["function"]["arguments"])
    assert args_bash["command"] == "pytest -v"
    assert "cmd" not in args_bash

    # bash with empty args defaults command to ""
    norm_empty_bash = normalize_tool_call("bash", {})
    assert norm_empty_bash is not None
    args_empty_bash = json.loads(norm_empty_bash["function"]["arguments"])
    assert args_empty_bash["command"] == ""


def test_skill_tool_aliasing_skips_own_name_without_dropping_it():
    """skill_view aliases skill_name/name/path (in that order) onto 'skill', but a
    candidate whose value equals the tool's own name (a redundant {"name":
    "skill_view", ...} echo) must be skipped rather than adopted, falling through to
    the next candidate — and because it was skipped rather than consumed, it must
    still be present afterwards (only a candidate actually used as the alias source
    gets popped)."""
    norm = normalize_tool_call("skill_view", {"name": "skill_view", "path": "grounded-citations"})
    assert norm is not None
    args = json.loads(norm["function"]["arguments"])
    assert args["skill"] == "grounded-citations"
    assert "path" not in args  # consumed as the alias source
    assert args.get("name") == "skill_view"  # skipped (matches the tool's own name), left untouched


def test_bare_dsml_closer_closes_stream_feed():
    """StreamToolParser must recognize bare </｜｜DSML｜｜> and </||DSML||> as closing tags
    during feed(), emitting the tool call without waiting for stream flush."""
    parser = StreamToolParser()
    chunk = (
        '<｜｜DSML｜｜ invoke name="read_file">'
        '<parameter name="path">/home/user/data.csv</parameter>'
        '</｜｜DSML｜｜>'
    )
    results = parser.feed(chunk)
    assert len(results) == 1
    assert "tool" in results[0]
    tool = results[0]["tool"]
    assert tool["function"]["name"] == "read_file"
    args = json.loads(tool["function"]["arguments"])
    assert args["path"] == "/home/user/data.csv"
    assert parser.in_tool is False


def test_batch_calls_unwrapping_in_tags_and_dict():
    """Batch shapes with 'calls' or 'tool_calls' arrays must be unpacked."""
    # In XML tag
    text = (
        '<tool_call>'
        '{"calls": ['
        '  {"name": "read_file", "arguments": {"path": "/a.txt"}},'
        '  {"name": "read_file", "arguments": {"path": "/b.txt"}}'
        ']}'
        '</tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    assert tools[0]["function"]["name"] == "read_file"
    assert json.loads(tools[0]["function"]["arguments"]) == {"path": "/a.txt"}
    assert tools[1]["function"]["name"] == "read_file"
    assert json.loads(tools[1]["function"]["arguments"]) == {"path": "/b.txt"}

    # Single call wrapped in calls array passed to normalize_tool_call
    single_batch = {
        "calls": [{"name": "bash", "arguments": {"command": "git status"}}]
    }
    norm = normalize_tool_call(single_batch)
    assert norm is not None
    assert norm["function"]["name"] == "bash"
    assert json.loads(norm["function"]["arguments"]) == {"command": "git status"}


def test_bare_parameter_tag_without_invoke_wrapper_is_unattributable():
    """A <parameter name=...> block with no enclosing invoke/tool_call opener
    carries no tool name anywhere in the text, so there is nothing to dispatch
    to — parse_tools correctly returns no tools rather than guessing, and the
    raw markup (stripped of the parameter tag itself) falls through as plain
    text. This is the exact shape reported from a live hermes-agent session
    where a tool call's opening wrapper was lost before reaching the bridge
    ("terminal output failure" pushing raw markup into chat) — captured here
    so a real fix (if the wrapper turns out to be recoverable) has a concrete
    regression case, and so _clean_text's leaked-markup warning (app.py) has
    a known trigger to log against."""
    text = (
        '<｜｜DSML｜｜ parameter name="code">\n'
        'import json\n'
        'print("hi")\n'
        '</｜｜DSML｜｜ parameter>'
    )
    tools, clean = parse_tools(text)
    assert tools == []
    assert "print" in clean

    import app
    from functions import _SUSPECT_LEAKED_TOOL_MARKUP_RE

    assert _SUSPECT_LEAKED_TOOL_MARKUP_RE.search(text)
    parsed_tools, clean_text = app._clean_text(text)
    assert parsed_tools == []
    assert "print" in clean_text

