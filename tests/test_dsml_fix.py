import json, sys, os, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import types
import importlib.util

# These fake modules exist only to satisfy functions.py/plugin_helper.py's
# imports when the real aiohttp/deepseek_tokenizer/wasmtime packages aren't
# installed (this file must be importable standalone in a minimal debug env).
#
# Each is only injected into sys.modules if it isn't already loaded AND isn't
# actually installed. Faking one that IS installed would get baked permanently
# into functions.py's/plugin_helper.py's module namespace on first import
# (Python binds `import aiohttp` to whatever object sys.modules['aiohttp'] is
# at that moment, for the life of the process) and desync from other test
# files — e.g. test_stage0_rails.py's real aiohttp.ClientConnectionError would
# no longer be caught by functions.py's `except aiohttp.ClientError` because
# the two files would be looking at unrelated exception hierarchies.
def _ensure_fake_module(name, build):
    if name in sys.modules:
        return
    try:
        found = importlib.util.find_spec(name) is not None
    except ImportError:
        found = False
    if found:
        return
    sys.modules[name] = build()


def _build_fake_aiohttp():
    aio = types.ModuleType('aiohttp')
    aio.ClientSession = type('CS', (), {
        '__init__': lambda *a, **k: None,
        '__aenter__': lambda s: s,
        '__aexit__': lambda *a: None,
        'post': lambda *a, **k: None,
    })
    aio.ClientTimeout = lambda **k: None
    class _MockClientError(Exception): pass
    class _MockClientConnectionError(_MockClientError): pass
    aio.ClientError = _MockClientError
    aio.ClientConnectionError = _MockClientConnectionError
    aio.HTTPError = _MockClientError
    aio.ContentTypeError = Exception
    return aio


def _build_fake_deepseek_tokenizer():
    dst = types.ModuleType('deepseek_tokenizer')
    dst.ds_token = types.SimpleNamespace(encode=lambda t: list(t))
    return dst


_ensure_fake_module('aiohttp', _build_fake_aiohttp)
_ensure_fake_module('deepseek_tokenizer', _build_fake_deepseek_tokenizer)
_ensure_fake_module('wasmtime', lambda: types.ModuleType('wasmtime'))

from functions import normalize_tool_call, parse_tools
from plugin_helper import enrich_tool_names, extract_tool_results


def test_dsml_numeric_coercion():
    r = normalize_tool_call('terminal', {'command': 'ls', 'timeout': '60\u003c/||DSML||>'})
    a = json.loads(r['function']['arguments'])
    assert a['timeout'] == 60
    assert isinstance(a['timeout'], int)


def test_string_number_coercion():
    r = normalize_tool_call('terminal', {'command': 'ls', 'timeout': '60'})
    a = json.loads(r['function']['arguments'])
    assert a['timeout'] == 60
    assert isinstance(a['timeout'], int)


def test_float_coercion():
    r = normalize_tool_call('terminal', {'command': 'ls', 'temperature': '0.7'})
    a = json.loads(r['function']['arguments'])
    assert a['temperature'] == 0.7
    assert isinstance(a['temperature'], float)


def test_pseudo_name_rejected():
    assert normalize_tool_call('tool_call', {}) is None
    assert normalize_tool_call('invoke', {}) is None
    assert normalize_tool_call('function_call', {}) is None


def test_nested_unwrap():
    raw = {
        'name': 'tool_call',
        'arguments': {
            'name': 'terminal',
            'arguments': {'command': 'ls'},
        },
    }
    r = normalize_tool_call(raw)
    assert r is not None
    assert r['function']['name'] == 'terminal'
    a = json.loads(r['function']['arguments'])
    assert a == {'command': 'ls'}


def test_parse_tools_with_dsml():
    text = (
        '<tool_call name="terminal">'
        '{"command": "ls", "timeout": "30\u003c/||DSML||>"}'
        '\u003c/tool_call>'
    )
    tools, clean = parse_tools(text)
    assert len(tools) == 1
    a = json.loads(tools[0]['function']['arguments'])
    assert a['timeout'] == 30
    assert isinstance(a['timeout'], int)


def test_enrich_tool_names():
    msgs = [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'skill_view', 'arguments': '{"skill": "A"}'}},
            {'id': 'c2', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command": "ls"}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result A'},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': 'result B'},
    ]
    enriched = enrich_tool_names(msgs)
    assert enriched[1]['name'] == 'skill_view'
    assert enriched[1]['tool_arguments'] == '{"skill": "A"}'
    assert enriched[2]['name'] == 'terminal'
    assert enriched[2]['tool_arguments'] == '{"command": "ls"}'


@pytest.mark.asyncio
async def test_extract_tool_results_sorting():
    msgs = [
        {'role': 'assistant', 'tool_calls': [
            {'id': 'c1', 'type': 'function', 'function': {'name': 'skill_view', 'arguments': '{"skill": "A"}'}},
            {'id': 'c2', 'type': 'function', 'function': {'name': 'terminal', 'arguments': '{"command": "ls"}'}},
        ]},
        {'role': 'tool', 'tool_call_id': 'c2', 'content': 'result B'},
        {'role': 'tool', 'tool_call_id': 'c1', 'content': 'result A'},
    ]
    enriched = enrich_tool_names(msgs)
    result = await extract_tool_results(enriched, latest_only=True)
    idx_c1 = result.index('(Call ID: c1)')
    idx_c2 = result.index('(Call ID: c2)')
    assert idx_c1 < idx_c2
    assert 'Tool: skill_view' in result
    assert 'Tool: terminal' in result


def test_stream_parser_dsml_inside_json_string():
    """StreamToolParser must NOT treat DSML closers inside JSON string values
    as real closing tags. The closer </||DSML||> inside {"timeout": "30</||DSML||>"}
    must be preserved, and the tool call extracted with the full JSON intact."""
    from functions import StreamToolParser
    parser = StreamToolParser()
    # Feed an opening tag, then JSON with DSML inside a string value,
    # then the real closing tag.
    opener = '<tool_call name="terminal">'
    json_body = '{"command": "ls", "timeout": "30</||DSML||>"}'
    closer = '</tool_call>'
    results = []
    results.extend(parser.feed(opener))
    results.extend(parser.feed(json_body))
    results.extend(parser.feed(closer))
    results.extend(parser.flush())

    tool_results = [r for r in results if "tool" in r]
    assert len(tool_results) == 1
    args = json.loads(tool_results[0]["tool"]["function"]["arguments"])
    assert args["command"] == "ls"
    assert args["timeout"] == 30
    assert isinstance(args["timeout"], int)


def test_stream_parser_dsml_inside_json_incremental():
    """Same as above but the JSON arrives in two chunks — the DSML marker
    straddles the chunk boundary. Parser must not treat the partial DSML
    as a closer."""
    from functions import StreamToolParser
    parser = StreamToolParser()
    results = []
    results.extend(parser.feed('<tool_call name="terminal">'))
    results.extend(parser.feed('{"command": "ls", "timeout": "30</||'))
    results.extend(parser.feed('DSML||>"}'))
    results.extend(parser.feed('</tool_call>'))
    results.extend(parser.flush())

    tool_results = [r for r in results if "tool" in r]
    assert len(tool_results) == 1
    args = json.loads(tool_results[0]["tool"]["function"]["arguments"])
    assert args["timeout"] == 30


def test_nested_envelope_with_empty_canonical_key():
    """Live hermes failure: execute_code arrived as
    {arguments: '{"code": "..."}', name: 'execute_code', code: ''} and hermes
    reported 'No code provided'. Collapse the envelope and drop the empty key."""
    raw = {
        "name": "execute_code",
        "arguments": {
            "arguments": '{"code": "print(1)"}',
            "name": "execute_code",
            "code": "",
        },
    }
    n = normalize_tool_call(raw)
    args = json.loads(n["function"]["arguments"])
    assert args["code"] == "print(1)"
    assert "arguments" not in args
    assert args.get("name") in (None, "execute_code")  # name echo must not hide code
    assert args["code"]


def test_empty_command_nested_arguments():
    raw = {
        "name": "terminal",
        "arguments": {
            "command": "",
            "arguments": {"command": "ls -la"},
            "name": "terminal",
        },
    }
    n = normalize_tool_call(raw)
    args = json.loads(n["function"]["arguments"])
    assert args["command"] == "ls -la"
    assert "arguments" not in args


def test_dsml_strip_does_not_eat_html():
    """The old DSML regex treated the marker as optional and then consumed
    [^>]*>, which deleted <div> from write_file content and execute_code."""
    n = normalize_tool_call("write_file", {
        "path": "/tmp/a.html",
        "content": "<div>hello</div>",
    })
    args = json.loads(n["function"]["arguments"])
    assert args["content"] == "<div>hello</div>"

    n2 = normalize_tool_call("execute_code", {"code": 'print("<div>x</div>")'})
    args2 = json.loads(n2["function"]["arguments"])
    assert args2["code"] == 'print("<div>x</div>")'


def test_write_file_missing_path_from_file_path_only():
    n = normalize_tool_call("write_file", {"file_path": "/tmp/x.py", "file_content": "x=1"})
    args = json.loads(n["function"]["arguments"])
    assert args["path"] == "/tmp/x.py"
    assert args["content"] == "x=1"


def test_stream_orphan_parameter_code_becomes_execute_code():
    from functions import StreamToolParser
    parser = StreamToolParser()
    chunk = (
        '<｜｜DSML｜｜ parameter name="code">'
        'print("hi")'
        '</｜｜DSML｜｜ parameter>'
    )
    results = parser.feed(chunk)
    results.extend(parser.flush())
    tools = [r["tool"] for r in results if "tool" in r]
    assert len(tools) == 1
    assert tools[0]["function"]["name"] == "execute_code"
    assert "print" in json.loads(tools[0]["function"]["arguments"])["code"]
