"""Regression tests for the streaming edge-case fixes (FIX 1/2/3 + DSML hold).

Covers the review items on StreamToolParser:

  FIX 1: flush() falls back to parse_tools(self.buffer) before stripping tags,
         so a stream cut off before the closing tag still yields the tool.
  FIX 2: feed() holds a trailing '<' only while it is a plausible partial match
         of _STREAM_ENTRY_RE (or a partial wrapper closer), so bare '<' prose
         keeps streaming.
  FIX 3: family-wide closer fallback (regex) accepts mismatched and |/｜-decorated
         closers during feed() instead of hanging until flush().
  FIX 4: the plausibility check keeps split DSML openers (any length, any
         bars/marker/spacing shape) buffered instead of leaking them as prose.

Run:  python tests/test_stream_edge_cases.py   (pytest-compatible)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from functions import StreamToolParser, _is_plausible_stream_entry_prefix  # noqa: E402
from functions import _is_plausible_stream_closer_prefix  # noqa: E402

OPEN = "<"  # guard against editor/tooling eating angle-bracket literals
CLOSE = ">"


def xml(text):
    return text.replace("[LT]", "<").replace("[GT]", ">")


def feed_all(parser, text, chunk_size):
    emitted_text, emitted_tools = [], []
    for i in range(0, len(text), chunk_size):
        for result in parser.feed(text[i : i + chunk_size]):
            if "text" in result:
                emitted_text.append(result["text"])
            else:
                emitted_tools.append(result["tool"])
    return "".join(emitted_text), emitted_tools


def flush_all(parser):
    emitted_text, emitted_tools = [], []
    for result in parser.flush():
        if "text" in result:
            emitted_text.append(result["text"])
        else:
            emitted_tools.append(result["tool"])
    return "".join(emitted_text), emitted_tools


def names(tools):
    return [t["function"]["name"] for t in tools]


def args_of(tools, index=0):
    import json

    return json.loads(tools[index]["function"]["arguments"])


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


def test_fix2_hold_and_flush_edge_cases():
    cases = [
        ("bare_lt", "if x < y then a > b", 2, 8),
        ("long_bare_lt", "if a < b and then c is larger than d " + "x" * 120, 3, 9),
        ("prefix_b", "1 < b and more prose", 1, 6),
        ("prefix_c", "1 < c and more prose", 1, 6),
        ("prefix_th", "1 < th and more prose", 1, 7),
        ("prefix_cat", "1 < cat and more prose", 1, 8),
        ("prefix_callsi", "1 < callsi and more prose", 1, 11),
        ("prefix_tool_", "use <tool_ in prose", 3, 12),
    ]
    for label, corpus, chunk_size, cutoff in cases:
        parser = StreamToolParser()
        streamed, tools = feed_all(parser, corpus, chunk_size)
        assert tools == [] and streamed == corpus and parser.buffer == "", label
        flush_text, flush_tools = flush_all(parser)
        assert flush_tools == [] and flush_text == "", label
        prefix_parser = StreamToolParser()
        head, head_tools = feed_all(prefix_parser, corpus[:cutoff], chunk_size)
        assert head_tools == [] and head == corpus[:cutoff] and prefix_parser.buffer == "", label
    eof_parser = StreamToolParser()
    before_eof, before_tools = feed_all(eof_parser, "1 < c", 1)
    assert before_tools == [] and before_eof == "1 " and eof_parser.buffer == "< c"
    tail_text, tail_tools = flush_all(eof_parser)
    assert tail_tools == [] and before_eof + tail_text == "1 < c"
    closer_parser = StreamToolParser()
    streamed_before, before_closer_tools = feed_all(closer_parser, "a</tool", 1)
    assert before_closer_tools == [] and streamed_before == "a" and closer_parser.buffer == "</tool"
    streamed_after, after_closer_tools = feed_all(closer_parser, "_x", 1)
    assert after_closer_tools == [] and streamed_after == "</tool_x" and closer_parser.buffer == ""
    closer_flush_text, closer_flush_tools = flush_all(closer_parser)
    assert closer_flush_tools == [] and closer_flush_text == ""
    empty_parser = StreamToolParser()
    assert empty_parser.feed("") == []
    held_text, held_tools = feed_all(empty_parser, "hi < ", 4)
    assert held_tools == [] and held_text == "hi " and empty_parser.buffer == "< "
    assert empty_parser.feed("") == []
    assert empty_parser.buffer == "< "
    empty_flush_text, empty_flush_tools = flush_all(empty_parser)
    assert empty_flush_tools == [] and held_text + empty_flush_text == "hi < "


def test_plausibility_helpers_track_entry_and_closer_grammar():
    entry_true = [
        "<", "< ", "<|", "<||", "<||D", "<||DS", "<||DSM", "<||DSML",
        "<||DSML|", "<||DSML||", "<||DSML|| ",
        "<||DSML|| i", "<||DSML|| invo", "<||DSML|| invoke",
        "<||DSML|| invoke ", "<||DSML|| invoke n", "<||DSML|| invoke na",
        "<||DSML|| invoke name=", '<||DSML|| invoke name="',
        '<||DSML|| invoke name="abc', '<||DSML|| invoke name="abc"',
        "< c", "< ca", "< calls", "< t", "< to", "< too",
        "<|invo", "<||dsml|| c", "<invoke ",
    ]
    entry_false = [
        "", "x", "<b", "< b", "< cat", "< callsign", "< callsi", "< th",
        "< invx", "< D", "< DSML", "<| D", "<|y|", "</", "</b", "</invoke",
        "<||DSML|||", '<||DSML|| invoke name="abc">',
    ]
    closer_true = [
        "</", "</ ", "</i", "</inv", "</invo", "</invoke", "</invoke ",
        "</tool", "</tool_", "</|", "</||DSML|| c", "</||DSML|| calls",
    ]
    closer_false = ["", "</b", "</ cat", "</tool_x", "</||DSML|| calr", "</x"]
    checks = (
        (_is_plausible_stream_entry_prefix, entry_true, True),
        (_is_plausible_stream_entry_prefix, entry_false, False),
        (_is_plausible_stream_closer_prefix, closer_true, True),
        (_is_plausible_stream_closer_prefix, closer_false, False),
    )
    for walker, segments, expected in checks:
        for segment in segments:
            assert walker(segment) is expected, segment


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


def _build_block(decor, spacing, tool_name, params, nested=False):
    opener = "<" + decor + spacing + 'invoke name="' + tool_name + '">'
    body = "".join(
        "<" + decor + spacing + 'parameter name="' + name + '" string="true">' + value
        + "</" + decor + spacing + "parameter>"
        for name, value in params.items()
    )
    closer = "</" + decor + spacing + "invoke>"
    if nested:
        opener = "<" + decor + spacing + "calls>" + opener
        closer = closer + "</" + decor + spacing + "calls>"
    return opener, opener + body + closer


def test_dsml_openers_parse_when_split_across_chunks():
    fullwidth_bar = "\uff5c"
    marker = fullwidth_bar * 2 + "DSML" + fullwidth_bar * 2
    mcp_tool = "mcp_pylance_mcp_s_pylanceCheckSignatureCompatibility"
    file_uri = {"fileUri": "functions.py"}
    dialect_shapes = [
        ("plain", marker, "", False),
        ("spaced", marker, " ", False),
        ("halfwidth_bars", "||DSML||", " ", False),
        ("no_decoration", "", "", False),
        ("single_bar", "|", "", False),
        ("mixed_bars", "|" + fullwidth_bar + "DSML" + fullwidth_bar + "|", "", False),
        ("lowercase_marker", "||dsml||", " ", False),
        ("nested_calls", marker, "", True),
    ]
    cases = []
    for label, decor, spacing, nested in dialect_shapes:
        opener, corpus = _build_block(decor, spacing, mcp_tool, file_uri, nested)
        cases.append((label, corpus, (1, max(len(opener) - 1, 1), len(opener)), mcp_tool, file_uri))
    long_tool = "mcp_" + "x" * 5000
    long_param = "p" * 300
    long_value = "v" * 5000
    opener, corpus = _build_block(marker, "", long_tool, {long_param: long_value})
    assert len(opener) > 5000
    cases.append(("unbounded_opener", corpus, (1, 7, len(opener) - 1, len(opener)), long_tool, {long_param: long_value}))
    cases.append((
        "json_body",
        "<" + marker + 'tool_call>{"name": "Bash", "arguments": {"command": "ls -la"}}</' + marker + "tool_call>",
        (1, 5, 11),
        "Bash",
        {"command": "ls -la"},
    ))
    for label, corpus, chunk_sizes, tool_name, tool_args in cases:
        for chunk_size in chunk_sizes:
            where = f"{label} chunk={chunk_size}"
            parser = StreamToolParser()
            streamed, tools = feed_all(parser, corpus, chunk_size)
            assert parser.buffer == "" and not parser.in_tool, where
            assert names(tools) == [tool_name], f"{where} {tools}"
            assert args_of(tools) == tool_args, where
            flush_text, flush_tools = flush_all(parser)
            assert flush_tools == [] and streamed + flush_text == "", where


TESTS = [
    test_fix1_flush_salvages_attribute_style_tool,
    test_fix1_flush_salvages_truncated_json_tool,
    test_fix1_flush_drops_wrapper_noise_after_json,
    test_fix1_flush_unparseable_still_strips_to_text,
    test_fix2_hold_and_flush_edge_cases,
    test_plausibility_helpers_track_entry_and_closer_grammar,
    test_fix3_mismatched_plain_closer,
    test_fix3_decorated_closer_halfwidth,
    test_fix3_decorated_closer_fullwidth,
    test_fix3_cross_family_closer,
    test_flush_is_terminal_and_resets_state,
    test_complete_blocks_unchanged,
    test_dsml_openers_parse_when_split_across_chunks,
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
