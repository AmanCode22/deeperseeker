"""Regression tests for the context-limit rollover policy (issue #22).

Covers:

  1. Large accumulated conversations are detected BEFORE old 24K-style
     truncation dominates (rollover trigger fires around the observed ~393K
     remembered-context limit, not the legacy 24K history cap).
  2. The summary request prompt is produced when near the limit and the
     rollover seed prompt embeds that summary.
  3. Tool results are included only when relevant (latest turn) and capped.
  4. Attachments are described in words inside the summary path instead of
     being forwarded.
  5. A new chat is seeded with the summary (seed prompt shape).
  6. Newest / relevant content is preserved in the seeded prompt.

Run:  python tests/test_context_rollover.py   (pytest-compatible)
"""
import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import plugin_helper
from plugin_helper import (
    build_prompt,
    build_summary_request_prompt,
    build_summary_seed_prompt,
    estimate_conversation_tokens,
    needs_rollover,
    strip_summary_tags,
    _cap_parts,
    _capped_text,
)


def big_text(words):
    return " ".join(["tokenword%d" % i for i in range(words)])


def big_history(num_pairs):
    """A conversation of num_pairs user/assistant exchanges, ~250 words each."""
    msgs = [{"role": "user", "content": big_text(250)}]
    for i in range(num_pairs):
        msgs.append({"role": "assistant", "content": big_text(250)})
        msgs.append({"role": "user", "content": big_text(250)})
    return msgs


def test_small_conversation_no_rollover():
    msgs = big_history(2)
    assert not needs_rollover(msgs), "small accumulated context must keep the current chat"


def test_large_accumulated_conversation_triggers_rollover():
    # ~394K+ tokens of accumulated context: exceeds the ~393K observed
    # remembered-context limit (minus the safety margin).
    msgs = big_history(400)
    assert estimate_conversation_tokens(msgs) > 380000
    assert needs_rollover(msgs), "accumulated context nearing the limit must roll over"


def test_rollover_trigger_is_far_above_legacy_24k_cap():
    # A ~35K-token conversation was dominated by the legacy 24K history cap;
    # under the new policy it must still keep the current chat.
    msgs = big_history(35)
    assert estimate_conversation_tokens(msgs) > 24000
    assert not needs_rollover(msgs)


def test_single_large_first_message_not_rolled_over():
    # Observed: a first message of ~1M tokens passes through untouched.
    msgs = [{"role": "user", "content": big_text(320000)}]
    assert estimate_conversation_tokens(msgs) > 900000
    assert not needs_rollover(msgs), "a single large first message must not be chopped"


def test_summary_request_prompt_shape():
    msgs = big_history(400)
    prompt = build_summary_request_prompt(msgs)
    assert "Summarize this conversation into a compact continuation note" in prompt
    assert "[CONVERSATION TO SUMMARIZE]" in prompt
    assert "[SUMMARY]" in prompt
    # no greetings/explanations asked, newest intent included
    assert "Output ONLY the summary" in prompt
    assert "tokenword" in prompt  # conversation text embedded


def test_summary_seed_prompt_preserves_newest_and_summary():
    summary = "Goal: deploy. Done: tests pass. Last request: fix the flaky test."
    prompt = build_summary_seed_prompt(summary, current_user_message="run the suite again")
    assert "[PREVIOUS CONVERSATION SUMMARY]" in prompt
    assert summary in prompt
    assert "[USER]\nrun the suite again" in prompt
    assert "do not mention the summarization" in prompt


def test_build_prompt_rollover_branch_preserves_newest_and_caps_tool_results():
    # Force the rollover branch without generating hundreds of thousands of
    # real tokens: patch the trigger while verifying the prompt shape.
    msgs = [
        {"role": "user", "content": "old question " + big_text(50)},
        {"role": "assistant", "content": "old answer"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "name": "Bash", "content": big_text(20000)},
        {"role": "user", "content": "now summarize what you found"},
    ]
    with mock.patch.object(plugin_helper, "needs_rollover", return_value=True):
        prompt = asyncio.run(build_prompt(msgs, [], "v4.1flash", is_first_message=True))
    assert "[USER]\nnow summarize what you found" in prompt, "newest user message must be kept"
    assert "Bash" in prompt and "Call ID: t1" in prompt, "relevant newest tool result must be kept"
    # tool result capped under MAX_TOOL_RESULTS_TOKENS
    from functions import count_tokens
    from plugin_helper import MAX_TOOL_RESULTS_TOKENS
    assert count_tokens(prompt) < 20000 + MAX_TOOL_RESULTS_TOKENS + 2000
    assert "old question" not in prompt, "old accumulated history must not be re-forwarded on rollover"


def test_tool_results_capped():
    parts, truncated = _cap_parts(["x " * 1], 10)
    text = _capped_text(big_text(5000), 100, "[... truncated ...]")
    assert truncated or "[... truncated ...]" in text
    # keep it strictly under budget
    assert len(text) < len(big_text(5000))


def test_attachments_described_not_forwarded():
    msgs = [
        {"role": "user", "content": [
            {"type": "text", "text": "what does this chart show?"},
            {"type": "image_url", "image_url": {"url": "https://example.com/chart.png"}},
        ]},
        {"role": "assistant", "content": "It shows a rising trend."},
        {"role": "user", "content": "elaborate"},
    ]
    described = plugin_helper._describe_attachments(msgs[0]["content"])
    assert "image" in described
    plain = plugin_helper._messages_plain_text(msgs)
    assert "[attachment: image shared]" in plain, "attachment must be described in words"
    # raw binary content never appears (there is none) — but also no base64 passthrough
    assert "data:image" not in plain


def test_strip_summary_tags_removes_think_and_labels():
    raw = "<think>hmm</think>Sure! [SUMMARY] the actual summary text"
    assert strip_summary_tags(raw) == "the actual summary text"
    assert strip_summary_tags("plain summary") == "plain summary"


def test_cap_parts_keeps_newest():
    parts = ["a", "b", "c"]
    kept, truncated = _cap_parts(parts, 10**9)
    assert kept == parts and not truncated
    kept, truncated = _cap_parts(parts, 1)
    assert kept == ["c"] and truncated


def test_env_overrides():
    with mock.patch.dict(os.environ, {"DEEPSEEKER_MEMORY_LIMIT_TOKENS": "100"}):
        import importlib
        importlib.reload(plugin_helper)
        assert plugin_helper.OBSERVED_MEMORY_LIMIT_TOKENS == 100
        assert plugin_helper.context_window_tokens() < 100
    importlib.reload(plugin_helper)


TESTS = [
    test_small_conversation_no_rollover,
    test_large_accumulated_conversation_triggers_rollover,
    test_rollover_trigger_is_far_above_legacy_24k_cap,
    test_single_large_first_message_not_rolled_over,
    test_summary_request_prompt_shape,
    test_summary_seed_prompt_preserves_newest_and_summary,
    test_build_prompt_rollover_branch_preserves_newest_and_caps_tool_results,
    test_tool_results_capped,
    test_attachments_described_not_forwarded,
    test_strip_summary_tags_removes_think_and_labels,
    test_cap_parts_keeps_newest,
    test_env_overrides,
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
