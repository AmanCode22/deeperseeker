import json, sys, os, asyncio
import pytest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import types
aio = types.ModuleType('aiohttp')
aio.ClientSession = type('CS', (), {
    '__init__': lambda *a, **k: None,
    '__aenter__': lambda s: s,
    '__aexit__': lambda *a: None,
    'post': lambda *a, **k: None,
})
aio.ClientTimeout = lambda **k: None
aio.ClientError = Exception
aio.HTTPError = Exception
aio.ContentTypeError = Exception
sys.modules['aiohttp'] = aio
dst = types.ModuleType('deepseek_tokenizer')
dst.ds_token = types.SimpleNamespace(encode=lambda t: list(t))
sys.modules['deepseek_tokenizer'] = dst
sys.modules['wasmtime'] = types.ModuleType('wasmtime')

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
