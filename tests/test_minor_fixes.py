"""Regression tests for the Stage 1 minor-fix batch.

* `minimal` is an explicit thinking-OFF effort value (OpenAI's gpt-5 default
  used to fall through by accident)
* /v1/responses accepts input_image parts (vision path already existed)
* empty/invalid JSON bodies answer 400, not 500
* the shared aiohttp session closes on lifespan shutdown

Run:  python tests/test_minor_fixes.py   (pytest-compatible)
"""
import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402
import app as app_module  # noqa: E402


def test_minimal_effort_disables_thinking():
    assert app_module.is_thinking_enabled({"effort": "minimal"}) is False
    assert app_module.is_thinking_enabled({"output_config": {"effort": "minimal"}}) is False
    assert app_module.is_thinking_enabled({"thinking": {"effort": "minimal"}}) is False
    assert app_module.is_thinking_enabled({"thinking": "minimal"}) is False
    assert app_module.is_thinking_enabled({"reasoning_effort": "minimal"}) is False


def test_responses_input_image_mapped():
    msgs = app_module._responses_input_to_messages([
        "plain question",
        {"role": "user", "content": [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": "https://example.com/x.png"},
            {"type": "input_image", "image_url": {"url": "data:image/png;base64,AAAA"}},
            {"type": "input_image", "file_id": "file-img"},
        ]},
    ])
    content = msgs[1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {"type": "image_url", "image_url": {"url": "https://example.com/x.png"}}
    assert content[2] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert content[3] == {"type": "file", "file_id": "file-img"}
    assert msgs[0]["content"] == "plain question"


def test_invalid_json_returns_400():
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    saved = app_module.check_key
    app_module.check_key = lambda request: True
    try:
        r = client.post("/v1/chat/completions", content=b"{definitely not json",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400, (r.status_code, r.text)
        r = client.post("/v1/chat/completions", content=b"",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400, (r.status_code, r.text)
        r = client.post("/v1/messages", content=b"[]",
                        headers={"content-type": "application/json"})
        assert r.status_code == 400, (r.status_code, r.text)
    finally:
        app_module.check_key = saved


def test_close_session_idempotent_and_resets():
    async def run():
        # No session open: must be a harmless no-op.
        await functions.close_session()
        assert functions._session is None
        # With an open session: closes and resets the handle.
        session = await functions.get_session()
        await functions.close_session()
        assert session.closed
        assert functions._session is None

    asyncio.run(run())


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
