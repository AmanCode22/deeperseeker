"""THINK fragment opened by a BATCH with a relative sub-op path.

Captured from DeepSeek (thinking enabled, after a READ_LINK fragment): the
reasoning fragment arrives as {"p":"response","o":"BATCH","v":[{"p":"fragments",
...}]}. It used to be dropped, so the first token was lost and the reasoning was
streamed as the visible answer instead of inside <think>.
"""

import asyncio
import json

from test_sse_upstream import _collect_send_message


def _body(*events):
    return "".join(
        "data: " + json.dumps(e, separators=(",", ":")) + "\n" for e in events
    ).encode()


def test_batch_relative_fragments_opens_think():
    body = _body(
        {"v": {"response": {"message_id": 4, "fragments": [{"id": 2, "type": "READ_LINK", "status": "WIP"}]}}},
        {"p": "response", "o": "BATCH", "v": [
            {"p": "fragments", "v": [{"id": 3, "type": "THINK", "content": "The"}]},
            {"p": "has_pending_fragment", "v": False},
        ]},
        {"p": "response/fragments/-1/content", "o": "APPEND", "v": " installed"},
        {"v": " version"},
        {"p": "response/fragments", "o": "APPEND", "v": [{"id": 4, "type": "RESPONSE", "content": "Antwort"}]},
        {"p": "response/fragments/-1/content", "v": " fertig"},
        {"p": "response", "o": "BATCH", "v": [
            {"p": "accumulated_token_usage", "v": 735},
            {"p": "quasi_status", "v": "FINISHED"},
        ]},
        {"p": "response/status", "o": "SET", "v": "FINISHED"},
    )
    out = asyncio.run(_collect_send_message(body))
    assert out == "<think>\nThe installed version\n</think>\n\nAntwort fertig"
