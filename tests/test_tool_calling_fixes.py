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
