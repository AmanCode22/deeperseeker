"""Regression tests for the 2026-09-06 local fixes.

Run with the repo venv:  deeperseeker_env/Scripts/python.exe tests/test_local_fixes.py
(also pytest-compatible).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import API_KEY, convert_anthropic_messages
from functions import StreamToolParser, parse_tools
from plugin_helper import build_prompt, generate_signature_sync


def test_api_key_never_empty():
    assert API_KEY, "API_KEY must never be empty (fail-open)"
    import importlib
    import unittest.mock as mock
    with mock.patch.dict(os.environ, {"DEEPSEEKER_API_KEY": ""}):
        import app
        importlib.reload(app)
        assert app.API_KEY, "empty DEEPSEEKER_API_KEY must fall back to the default"
    importlib.reload(app)


def test_tool_result_becomes_tool_role():
    msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"},
        ]},
    ]
    out = convert_anthropic_messages(msgs)
    assert out[1]["tool_calls"][0]["function"]["name"] == "Bash"
    assert out[2]["role"] == "tool", "tool_result must become a role=tool message, not user text"
    assert out[2]["tool_call_id"] == "toolu_1"
    assert out[2]["content"] == "file.txt"


def test_signature_matches_server_reconstruction():
    # What the server reconstructs after parsing model output (as stream_response does)
    model_output = 'Working on it.\n<tool_call>{"name": "Bash", "arguments": {"command": "ls -la"}}</tool_call>'
    parsed_tools, clean_text = parse_tools(model_output)
    assert parsed_tools, "parse_tools should find the tool call"
    server_msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "tool_calls": parsed_tools},
    ]
    # What an Anthropic client echoes back on the next turn
    client_msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Working on it."},
            {"type": "tool_use", "id": "toolu_abc", "name": "Bash", "input": {"command": "ls -la"}},
        ]},
    ]
    converted = convert_anthropic_messages(client_msgs)
    assert generate_signature_sync(server_msgs, "expert") == generate_signature_sync(converted, "expert"), \
        "signature cache must hit when the client echoes the assistant tool turn"


def test_tool_results_reach_build_prompt():
    msgs = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "file.txt"},
        ]},
    ]
    converted = convert_anthropic_messages(msgs)
    prompt = asyncio.run(build_prompt(converted, [], "expert", is_first_message=False))
    assert "[TOOL RESULTS]" in prompt, "tool results must appear in the [TOOL RESULTS] section"
    assert "file.txt" in prompt
    assert "[USER]" not in prompt, "the original question must not be re-sent on follow-up turns"


def test_user_text_after_tool_result_preserved():
    msgs = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.txt"},
            {"type": "text", "text": "now list the hidden files"},
        ]},
    ]
    out = convert_anthropic_messages(msgs)
    assert out[0]["role"] == "assistant" and out[0]["tool_calls"]
    assert out[1]["role"] == "tool"
    assert out[2]["role"] == "user"
    assert out[2]["content"] == "now list the hidden files"


def test_dsml_tool_call_extracts_arguments():
    """DSH/deepseek-harness emits DSML with a space after the ｜ marker, e.g.
    '<｜｜DSML｜｜ parameter name="command" ...>'. parse_tools must extract the argument."""
    text = (
        '<｜｜DSML｜｜calls>'
        '<｜｜DSML｜｜invoke name="pwsh">'
        '<｜｜DSML｜｜ parameter name="command" string="true">Get-Location</｜｜DSML｜｜parameter>'
        '</｜｜DSML｜｜ invoke>'
        '</｜｜DSML｜｜ calls>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 1, f"expected one tool, got {tools}"
    assert tools[0]["function"]["name"] == "pwsh"
    args = tools[0]["function"]["arguments"]
    assert '"Get-Location"' in args, f"arguments lost: {args}"
    assert clean == "", f"DSML residue left in clean_text: {clean!r}"


def test_dsml_multi_tool_arguments():
    text = (
        '<｜｜DSML｜｜calls>'
        '<｜｜DSML｜｜invoke name="pwsh">'
        '<｜｜DSML｜｜ parameter name="command" string="true">ls</｜｜DSML｜｜ parameter>'
        '</｜｜DSML｜｜ invoke>'
        '<｜｜DSML｜｜ invoke name="read">'
        '<｜｜DSML｜｜ parameter name="file_path" string="true">a.txt</｜｜DSML｜｜ parameter>'
        '</ invoke>'
        '</｜｜DSML｜｜ calls>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 2
    assert tools[0]["function"]["name"] == "pwsh"
    assert '"ls"' in tools[0]["function"]["arguments"]
    assert tools[1]["function"]["name"] == "read"
    assert '"a.txt"' in tools[1]["function"]["arguments"]
    assert clean == ""


def test_stream_parser_dsml_across_chunks():
    """StreamToolParser must recognise DSML entry tags and emit the tool once
    the outer </｜｜DSML｜｜calls> closes, without leaking tag text."""
    chunks = [
        "привет\n\n",
        "<｜｜DSML｜｜calls>",
        '<｜｜DSML｜｜invoke name="pwsh">',
        '<｜｜DSML｜｜ parameter name="command" string="true">ls',
        '</｜｜DSML｜｜ parameter>',
        '</ invoke>',
        '</｜｜DSML｜｜ calls>',
    ]
    p = StreamToolParser()
    text_out = ""
    tool_calls = []
    for c in chunks:
        for item in p.feed(c):
            if "text" in item:
                text_out += item["text"]
            elif "tool" in item:
                tool_calls.append(item["tool"])
    for item in p.flush():
        if "text" in item:
            text_out += item["text"]
    assert text_out == "привет\n\n", f"unexpected text residue: {text_out!r}"
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "pwsh"
    assert '"ls"' in tool_calls[0]["function"]["arguments"]
    assert "</" not in text_out and "｜｜DSML｜｜" not in text_out


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
