"""A stream DeepSeek drops before any visible output is retried on another account."""

import asyncio

from fastapi.responses import StreamingResponse

import app as app_module


async def _noop_db(fn, *args, **kwargs):
    return None


def _run(gen_parts, monkeypatch):
    calls = []

    async def upstream():
        for p in gen_parts:
            yield p
        raise ConnectionResetError(104, "Connection reset by peer")

    async def retried_body():
        yield "data: retried\n\n"

    async def retry(failed_token):
        calls.append(failed_token)
        return StreamingResponse(retried_body(), media_type="text/event-stream")

    monkeypatch.setattr(app_module, "_db", _noop_db)

    async def collect():
        token = app_module._stream_retry.set(retry)
        try:
            out = []
            async for part in app_module.stream_response(
                upstream(), "deepseek-flash", [{"role": "user", "content": "hi"}], 7, "s", "sig", None
            ):
                out.append(part)
            return "".join(out)
        finally:
            app_module._stream_retry.reset(token)

    return asyncio.run(collect()), calls


def test_drop_during_reasoning_is_retried(monkeypatch):
    out, calls = _run(["<think>planning the call"], monkeypatch)
    assert calls == [7]
    assert "data: retried" in out
    assert '"error"' not in out


def test_drop_after_visible_text_reports_error(monkeypatch):
    out, calls = _run(["Here is the answer so far"], monkeypatch)
    assert calls == []
    assert '"error"' in out and "Connection reset" in out
