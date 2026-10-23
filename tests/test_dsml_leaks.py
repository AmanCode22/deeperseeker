"""Malformed DSML seen live from deepseek-web via omp (2026-10-23)."""

import asyncio
import json

from functions import StreamToolParser, parse_tools, request_tools
from plugin_helper import extract_tool_results

B = "｜｜DSML｜｜"


def _args(text):
    tools, clean = parse_tools(text)
    assert len(tools) == 1, tools
    return json.loads(tools[0]["function"]["arguments"]), clean


def test_slash_opener_for_next_parameter():
    text = (
        f'<{B} calls>\n<{B} invoke name="bash">\n'
        f'<{B} parameter name="i" string="true">Reading code</{B} parameter name="command" string="true">ls -la'
        f"</{B} parameter>\n</{B} invoke>\n</{B} calls>"
    )
    args, _ = _args(text)
    assert args == {"i": "Reading code", "command": "ls -la"}


def test_bare_and_truncated_closers_end_value():
    text = (
        f'<{B} calls>\n<{B} invoke name="bash">\n'
        f'<{B} parameter name="command" string="true">head -c 3000</{B}>\n</{B}>\n'
        f'<{B} parameter name="timeout" string="false">180</{B} </{B} </{B} calls>'
    )
    args, _ = _args(text)
    assert args == {"command": "head -c 3000", "timeout": 180}


def test_closer_without_angle_bracket():
    text = (
        f'<{B}function_calls>\n<{B}invoke name="read">\n'
        f'<{B}parameter name="path" string="true">src/dashboard.ts:1-140:raw{B}parameter>\n'
        f"</{B}invoke>\n</{B}function_calls>"
    )
    args, _ = _args(text)
    assert args == {"path": "src/dashboard.ts:1-140:raw"}


def test_string_true_keeps_literal():
    text = (
        f'<{B}function_calls>\n<{B}invoke name="write">\n'
        f'<{B}parameter name="content" string="true">42</{B}parameter>\n'
        f"</{B}invoke>\n</{B}function_calls>"
    )
    args, _ = _args(text)
    assert args == {"content": "42"}


def test_truncated_markers_not_streamed_as_text():
    p = StreamToolParser()
    out = p.feed(f"Probing now.<{B} </{B} </{B} ") + p.flush()
    text = "".join(o.get("text", "") for o in out)
    assert "DSML" not in text and "Probing now." in text


def test_stream_multi_invoke():
    p = StreamToolParser()
    s = (
        f'<{B}function_calls>\n<{B}invoke name="a">\n<{B}parameter name="x" string="true">1</{B}parameter>\n</{B}invoke>\n'
        f'<{B}invoke name="b">\n<{B}parameter name="y" string="false">2</{B}parameter>\n</{B}invoke>\n</{B}function_calls>'
    )
    out = []
    for i in range(0, len(s), 7):
        out += p.feed(s[i : i + 7])
    out += p.flush()
    tools = [o["tool"]["function"] for o in out if "tool" in o]
    assert [(t["name"], json.loads(t["arguments"])) for t in tools] == [
        ("a", {"x": "1"}),
        ("b", {"y": 2}),
    ]
    assert not "".join(o.get("text", "") for o in out).strip()


def test_parameter_opener_without_parameter_name():
    text = (
        f'<{B}function_calls>\n<{B}invoke name="bash">\n'
        f'<{B}command" string="true">git show 0851eb5 --stat</{B}parameter>\n'
        f'<{B}i" string="true">Inspecting diff</{B}parameter>\n'
        f"</{B}invoke>"
    )
    args, _ = _args(text)
    assert args == {"command": "git show 0851eb5 --stat", "i": "Inspecting diff"}


def tool(name, props, required):
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
    tool("read", ["i", "path", "offset", "limit"], ["path"]),
    tool("grep", ["i", "pattern", "path"], ["pattern"]),
    tool("bash", ["i", "command", "timeout"], ["command"]),
    tool("edit", ["i", "path", "old_string", "new_string"], ["path", "old_string", "new_string"]),
]


def test_missing_invoke_name_inferred_from_request_tools():

    token = request_tools.set(TOOLS)
    try:
        text = (
            "<think>\nread both\n</think>\n\n"
            f'<{B}function_calls>\n<{B} parameter name="i" string="true">Reading pool.ts head</{B} parameter>\n'
            f'<{B} parameter name="path" string="true">src/pool.ts:1-60</{B} parameter>\n</{B} invoke>\n'
            f'<｜DSML｜｜function_calls>\n<{B} parameter name="i" string="true">Reading server.ts</{B} parameter>\n'
            f'<{B} parameter name="path" string="true">src/server.ts:1-230</{B} parameter>\n</{B} invoke>\n'
            f"</{B}＞</{B}function_calls>"
        )
        tools, clean = parse_tools(text)
        assert [(t["function"]["name"], json.loads(t["function"]["arguments"])["path"]) for t in tools] == [
            ("read", "src/pool.ts:1-60"),
            ("read", "src/server.ts:1-230"),
        ]
        assert "DSML" not in clean
    finally:
        request_tools.reset(token)


def test_fused_invoke_and_first_parameter():
    token = request_tools.set(TOOLS)
    try:
        text = (
            f'<{B}function_calls>\n<{B}invoke name="i" string="true">Locating LoginManager'
            f'</{B}parameter name="path" string="true">./src/login.ts'
            f'</{B}parameter name="pattern" string="true">class LoginManager</{B}parameter>\n<{B}invoke>'
        )
        tools, _ = parse_tools(text)
        assert [(t["function"]["name"], json.loads(t["function"]["arguments"])) for t in tools] == [
            ("grep", {"i": "Locating LoginManager", "path": "./src/login.ts", "pattern": "class LoginManager"})
        ]
    finally:
        request_tools.reset(token)


def test_closer_with_bars_after_dsml_only():
    text = (
        f'<{B}function_calls>\n<{B}invoke name="read">\n'
        f'<{B}parameter name="i" string="true">Reading ad failure branch</DSML｜｜>\n'
        f'<{B}parameter name="path" string="true">src/pool.ts:280-300</{B}parameter>\n</{B}invoke>'
    )
    args, _ = _args(text)
    assert args == {"i": "Reading ad failure branch", "path": "src/pool.ts:280-300"}


def test_tool_results_echo_call_arguments():
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": '{"path": "a.ts"}'}},
                {"id": "c2", "type": "function", "function": {"name": "read", "arguments": '{"path": "b.ts"}'}},
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "content": "B"},
        {"role": "tool", "tool_call_id": "c1", "content": "A"},
    ]
    out = asyncio.run(extract_tool_results(messages, latest_only=True))
    assert 'Tool: read (Call ID: c2)\nArguments: {"path": "b.ts"}\nResult: B' in out
    assert 'Tool: read (Call ID: c1)\nArguments: {"path": "a.ts"}\nResult: A' in out


def test_parameter_without_name_attribute():
    text = (
        f'<{B}invoke name="grep">\n'
        f'<{B} parameter name="i" string="true">Searching maxRequestBodySize</{B} parameter="pattern" string="true">maxRequestBodySize|idleTimeout</{B} parameter>\n'
        f'<{B} parameter name="path" string="true">src/</{B} parameter>\n</{B}invoke>'
    )
    args, _ = _args(text)
    assert args == {"i": "Searching maxRequestBodySize", "pattern": "maxRequestBodySize|idleTimeout", "path": "src/"}


def test_invoke_less_block_then_slash_invoke():
    token = request_tools.set(TOOLS)
    try:
        text = (
            f'<{B} calls>\n<{B} parameter name="command" string="true">bun run stub.ts'
            f'</{B} parameter name="i" string="true">Running stub</{B}>\n</{B}>\n'
            f'<{B} calls>\n</{B} invoke name="bash">\n'
            f'<{B} parameter name="command" string="true">curl -s localhost'
            f'</{B} parameter name="i" string="true">Testing</{B}>\n</{B}>\n</{B} calls>'
        )
        tools, clean = parse_tools(text)
        assert [(t["function"]["name"], json.loads(t["function"]["arguments"])) for t in tools] == [
            ("bash", {"command": "bun run stub.ts", "i": "Running stub"}),
            ("bash", {"command": "curl -s localhost", "i": "Testing"}),
        ]
        assert "DSML" not in clean
    finally:
        request_tools.reset(token)


def test_parameter_name_as_tag_with_type_value():
    text = (
        f'<{B} calls>\n<{B} invoke name="edit">\n'
        f'<{B} parameter name="i" string="true">Sanitizing message</{B} parameter>\n'
        f'<{B} old_string="true">  const a = 1;</｜DSML｜｜ parameter>\n'
        f'<{B} new_string="true">  const a = 2;</｜DSML｜｜ parameter>\n'
        f'<{B} path="true">src/freepi.ts</{B}>\n</{B} invoke>\n'
        f'<{B} calls>\n<{B} invoke name="bash">\n'
        f'<{B} command="true">grep -n "res.json()" src/freepi.ts</{B} i="Finding reads"{B}>\n</{B} calls>'
    )
    tools, _ = parse_tools(text)
    got = [(t["function"]["name"], json.loads(t["function"]["arguments"])) for t in tools]
    assert got == [
        ("edit", {"i": "Sanitizing message", "old_string": "const a = 1;", "new_string": "const a = 2;", "path": "src/freepi.ts"}),
        ("bash", {"command": 'grep -n "res.json()" src/freepi.ts'}),
    ]


def test_invoke_less_edit_missing_required_path_still_named():
    token = request_tools.set(TOOLS)
    try:
        text = (
            f'<｜DSML｜｜ calls>\n<｜DSML｜｜ parameter name="i" string="true">Deriving constant'
            f'</｜DSML｜｜ parameter name="new_string" string="true">const A = 1;\nlet s;</｜DSML｜｜ parameter>\n'
            f'<｜DSML｜｜ parameter="old_string" string="true">let s;</｜DSML｜｜>\n</{B}>\n</{B}>'
        )
        tools, _ = parse_tools(text)
        assert [(t["function"]["name"], sorted(json.loads(t["function"]["arguments"]))) for t in tools] == [
            ("edit", ["i", "new_string", "old_string"])
        ]
    finally:
        request_tools.reset(token)


def test_bare_invoke_without_name_is_inferred():
    token = request_tools.set(TOOLS)
    try:
        text = (
            f'<{B}invoke>\n<{B}parameter name="i" string="true">Checking enable control'
            f'</{B}parameter name="path" string="true">src/dashboard.ts</{B}parameter>\n'
            f'<{B}parameter name="pattern" string="true">disabled|enable</{B}parameter>\n</{B}invoke>'
        )
        tools, _ = parse_tools(text)
        assert [(t["function"]["name"], json.loads(t["function"]["arguments"])) for t in tools] == [
            ("grep", {"i": "Checking enable control", "path": "src/dashboard.ts", "pattern": "disabled|enable"})
        ]
    finally:
        request_tools.reset(token)
