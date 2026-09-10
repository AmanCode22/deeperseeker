"""Regression tests for the streaming edge-case fixes (FIX 1/2/3).

Covers the three review items on StreamToolParser:

  FIX 1: flush() falls back to parse_tools(self.buffer) before stripping tags,
         so a stream cut off before the closing tag still yields the tool.
  FIX 2: the tag-prefix hold is explicitly bounded (_MAX_TAG_HOLD) and bare '<'
         prose keeps streaming instead of buffering until flush().
  FIX 3: family-wide closer fallback (regex) accepts mismatched and |/｜-decorated
         closers during feed() instead of hanging until flush().

Run:  python tests/test_stream_edge_cases.py   (pytest-compatible)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions import StreamToolParser, _MAX_TAG_HOLD  # noqa: E402

OPEN = "<"  # guard against editor/tooling eating angle-bracket literals
CLOSE = ">"


def xml(s):
    return s.replace("[LT]", "<").replace("[GT]", ">")


def feed_all(parser, text, size):
    text_out, tools_out = [], []
    for i in range(0, len(text), size):
        for r in parser.feed(text[i : i + size]):
            if "text" in r:
                text_out.append(r["text"])
            else:
                tools_out.append(r["tool"])
    return "".join(text_out), tools_out


def flush_all(parser):
    text_out, tools_out = [], []
    for r in parser.flush():
        if "text" in r:
            text_out.append(r["text"])
        else:
            tools_out.append(r["tool"])
    return "".join(text_out), tools_out


def names(tools):
    return [t["function"]["name"] for t in tools]


def args_of(tools, i=0):
    import json

    return json.loads(tools[i]["function"]["arguments"])


def test_fix1_flush_salvages_attribute_style_tool():
    """Attribute-style tool cut off before the closer: flush() must emit the tool,
    not dump raw parameter values as chat text."""
    corpus = xml('[LT]tool_call name="Bash"[GT][LT]parameter name="command"[GT]ls -la[LT]/parameter[GT]')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 7)
    assert tools == [] and text == "", "nothing should be emitted before flush"
    f_text, f_tools = flush_all(p)
    assert names(f_tools) == ["Bash"], f"expected tool from flush, got {f_tools}"
    assert args_of(f_tools) == {"command": "ls -la"}
    assert f_text == "", f"no parameter values may leak as text, got {f_text!r}"


def test_fix1_flush_salvages_truncated_json_tool():
    """JSON-style tool whose arguments JSON is brace-truncated: flush() recovers
    the tool via parse_tools' brace-balancing fallback."""
    corpus = xml('[LT]tool_call[GT]{"name": "Bash", "arguments": {"command": "ls"')
    p = StreamToolParser()
    feed_all(p, corpus, 5)
    f_text, f_tools = flush_all(p)
    assert names(f_tools) == ["Bash"], f"expected salvaged tool, got {f_tools}"
    assert args_of(f_tools) == {"command": "ls"}


def test_fix1_flush_drops_wrapper_noise_after_json():
    """Tool already emitted via the JSON path; leftover partial closer at EOF must
    not leak into chat text."""
    corpus = xml('[LT]tool_call[GT]{"name": "Bash", "arguments": {}}[LT]/tool_')
    p = StreamToolParser()
    _, tools = feed_all(p, corpus, 9)
    assert names(tools) == ["Bash"]
    f_text, f_tools = flush_all(p)
    assert f_tools == [] and f_text == "", f"wrapper noise leaked: {f_text!r}"


def test_fix1_flush_unparseable_still_strips_to_text():
    """When nothing parses (no name attribute, no JSON), legacy behaviour stands:
    strip wrapper tags, dump the remainder as text."""
    corpus = xml('[LT]tool_call[GT]not a tool body')
    p = StreamToolParser()
    feed_all(p, corpus, 4)
    f_text, f_tools = flush_all(p)
    assert f_tools == []
    assert f_text == "not a tool body", repr(f_text)


def test_fix2_prose_with_bare_lt_streams_immediately():
    corpus = "if x < y then a > b"
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 2)
    assert tools == []
    assert text == corpus, f"bare '<' must not buffer prose, got {text!r}"
    f_text, _ = flush_all(p)
    assert f_text == ""


def test_fix2_hold_is_bounded():
    """A tail that keeps looking like a tag prefix can never buffer more than
    _MAX_TAG_HOLD characters."""
    assert _MAX_TAG_HOLD <= 15, "review asked for a ~15-char bound"
    corpus = "a <" + "z" * 200
    p = StreamToolParser()
    text, _ = feed_all(p, corpus, 3)
    f_text, _ = flush_all(p)
    assert text + f_text == corpus


def test_fix2_partial_prefix_released_as_prose():
    corpus = xml("use [LT]tool_ in prose")
    p = StreamToolParser()
    text, _ = feed_all(p, corpus, 3)
    f_text, _ = flush_all(p)
    assert text + f_text == corpus


def test_fix3_mismatched_plain_closer():
    """</tool_calls> must close a <tool_call> block during feed (regression lock)."""
    corpus = xml('[LT]tool_call[GT]{"name": "Bash", "arguments": {"command": "ls"}}[LT]/tool_calls[GT]')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 5)
    assert names(tools) == ["Bash"], f"mismatched closer must not hang, got {tools}"
    f_text, f_tools = flush_all(p)
    assert f_tools == [] and text == ""


def test_fix3_decorated_closer_halfwidth():
    """</|tool_call|> must close the block during feed, not just by JSON accident."""
    corpus = xml('[LT]tool_call name="Bash"[GT][LT]parameter name="command"[GT]ls[LT]/parameter[GT][LT]/|tool_call[GT]')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 6)
    assert names(tools) == ["Bash"], f"decorated closer must close block, got {tools}"
    assert args_of(tools) == {"command": "ls"}
    f_text, f_tools = flush_all(p)
    assert f_tools == [] and text == ""


def test_fix3_decorated_closer_fullwidth():
    """</｜tool_call｜> (fullwidth bars) must also close the block during feed."""
    corpus = xml('[LT]tool_call name="Read"[GT][LT]parameter name="path"[GT]/tmp/a[LT]/parameter[GT][LT]/｜tool_call[GT]')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 4)
    assert names(tools) == ["Read"], tools
    assert args_of(tools) == {"path": "/tmp/a"}


def test_fix3_cross_family_closer():
    """</invoke> must close a <function_call> block (family-wide fallback)."""
    corpus = xml('[LT]function_call[GT]{"name": "Bash", "arguments": {}}[LT]/invoke[GT]')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 7)
    assert names(tools) == ["Bash"], tools


def test_flush_is_terminal_and_resets_state():
    p = StreamToolParser()
    feed_all(p, xml('[LT]tool_call name="Bash"[GT][LT]parameter name="command"[GT]ls[LT]/parameter[GT]'), 5)
    p.flush()
    assert p.buffer == "" and not p.in_tool and not p.json_done
    assert p.flush() == []


def test_complete_blocks_unchanged():
    """Guard: happy-path behaviour is untouched."""
    corpus = xml('Working.[LT]tool_call[GT]{"name": "Bash", "arguments": {"command": "ls -la"}}[LT]/tool_call[GT]Done.')
    p = StreamToolParser()
    text, tools = feed_all(p, corpus, 6)
    assert names(tools) == ["Bash"] and args_of(tools) == {"command": "ls -la"}
    f_text, f_tools = flush_all(p)
    assert text + f_text == "Working.Done."
    assert f_tools == []


TESTS = [
    test_fix1_flush_salvages_attribute_style_tool,
    test_fix1_flush_salvages_truncated_json_tool,
    test_fix1_flush_drops_wrapper_noise_after_json,
    test_fix1_flush_unparseable_still_strips_to_text,
    test_fix2_prose_with_bare_lt_streams_immediately,
    test_fix2_hold_is_bounded,
    test_fix2_partial_prefix_released_as_prose,
    test_fix3_mismatched_plain_closer,
    test_fix3_decorated_closer_halfwidth,
    test_fix3_decorated_closer_fullwidth,
    test_fix3_cross_family_closer,
    test_flush_is_terminal_and_resets_state,
    test_complete_blocks_unchanged,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
