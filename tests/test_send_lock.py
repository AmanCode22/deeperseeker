"""Per-account send lock must not leak when a consumer abandons a stream (2026-10-23)."""

import asyncio

import functions


def test_abandoned_stream_releases_account_lock(monkeypatch):
    async def fake_unlocked(chat_id, auth_token, *args, **kwargs):
        for i in range(3):
            await asyncio.sleep(0)
            yield f"{chat_id}-{i}"

    monkeypatch.setattr(functions, "_send_message_unlocked", fake_unlocked)
    monkeypatch.setattr(functions, "_send_locks", {})

    async def run():
        first = functions.send_message("a", "tok")
        assert await first.__anext__() == "a-0"
        # Abandon `first` without aclose (client gone, iterator never closed) and keep
        # a reference so GC can't finalize it.
        second = [c async for c in functions.send_message("b", "tok")]
        assert second == ["b-0", "b-1", "b-2"]
        return first

    asyncio.run(asyncio.wait_for(run(), 5))


def test_upstream_error_reaches_consumer(monkeypatch):
    async def fake_unlocked(chat_id, auth_token, *args, **kwargs):
        yield "x"
        raise functions.UpstreamError(502, "boom")

    monkeypatch.setattr(functions, "_send_message_unlocked", fake_unlocked)
    monkeypatch.setattr(functions, "_send_locks", {})

    async def run():
        got = []
        try:
            async for c in functions.send_message("a", "tok"):
                got.append(c)
        except functions.UpstreamError as e:
            return got, str(e)

    got, err = asyncio.run(run())
    assert got == ["x"] and "boom" in err
