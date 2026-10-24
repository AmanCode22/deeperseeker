"""Effort above medium becomes a think-harder instruction (DeepSeek web has only on/off)."""

import asyncio

from functions import effort_level, request_effort
from plugin_helper import build_prompt


def test_effort_level_dialects():
    assert effort_level({"reasoning_effort": "High"}) == "high"
    assert effort_level({"reasoning": {"effort": "xhigh"}}) == "xhigh"
    assert effort_level({"thinking": {"type": "enabled", "budget_tokens": 9}}) is None
    assert effort_level({}) is None


def _prompt(effort):
    token = request_effort.set(effort)
    try:
        msgs = [{"role": "system", "content": "be helpful"}, {"role": "user", "content": "hi"}]
        return asyncio.run(build_prompt(msgs, [], "deepseek-flash", is_first_message=True))
    finally:
        request_effort.reset(token)


def test_instruction_per_effort():
    assert "[REASONING EFFORT" not in _prompt(None)
    assert "[REASONING EFFORT" not in _prompt("medium")
    assert "[REASONING EFFORT" not in _prompt("high")
    assert "[REASONING EFFORT: max]" in _prompt("max")
    assert "[REASONING EFFORT: max]" in _prompt("xhigh")
