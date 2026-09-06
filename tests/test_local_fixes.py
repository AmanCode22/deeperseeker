"""Regression tests for the 2026-09-06 local fixes.

Run with the repo venv:  deeperseeker_env/Scripts/python.exe tests/test_local_fixes.py
(also pytest-compatible).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import API_KEY, convert_anthropic_messages
from functions import parse_tools
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
