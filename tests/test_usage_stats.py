"""Regression tests for the usage-stats aggregation (P0-6, Days 28-30).

The 5-line spec locked by this file:

  1. per-day x per-key rows of requests / input tokens / output tokens /
     cost, honest zeros where a key saw no traffic that day (it simply has
     no rows — the page must never crash on an empty day)
  2. streamed retries count ONCE: failed attempts record nothing; the
     completed turn records exactly one request
  3. tokens are billed on the CLEANED completion (B7 semantics) — the same
     numbers the client-visible usage chunk reports
  4. cost is derived from the flat tariff table shipped with the bridge and
     labeled as such (pricing-source note), never presented as metering
  5. key aliases are masked everywhere; no key material reaches the page

Run:  python tests/test_usage_stats.py   (pytest-compatible)
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402
import functions  # noqa: E402

# Fixed "today" so the day buckets are deterministic: 2026-10-21 UTC.
T0 = 1792540800.0  # 2026-10-21 00:00:00 UTC
DAY = "2026-10-21"
DAY2 = "2026-10-19"


def _fresh_store():
    tmp = tempfile.mkdtemp()
    saved = functions._db
    functions._db = os.path.join(tmp, "deeperseeker.db")
    functions.init_db()
    return saved


def test_every_cell_of_the_expected_table():
    """2 keys x 3 days of known records; every cell asserted."""
    saved = _fresh_store()
    try:
        # key A: 3 records on DAY (2 openai, 1 anthropic), 1 record on DAY2
        functions.record_usage("dsk-aaaa1111", "openai", "v4.1flash", 100, 50, ts=T0)
        functions.record_usage("dsk-aaaa1111", "openai", "v4.1flash", 200, 150, ts=T0)
        functions.record_usage(
            "dsk-aaaa1111",
            "anthropic",
            "v4.1flash",
            10,
            5,
            ts=T0 - 2 * 86400,
        )
        # key B: 1 record on DAY
        functions.record_usage("dsk-bbbb2222", "openai", "v4.1flash", 7, 3, ts=T0)

        rows = functions.usage_by_day_key()
        by = {(r["day"], r["key"]): r for r in rows}
        assert ("2026-10-21", "…1111") in by, by
        assert ("2026-10-21", "…2222") in by
        assert ("2026-10-19", "…1111") in by

        a_today = by[("2026-10-21", "…1111")]
        assert a_today["requests"] == 2
        assert a_today["input_tokens"] == 300
        assert a_today["output_tokens"] == 200
        # cost = 300/1M*0.44 + 200/1M*1.32 = 0.000132 + 0.000264 = 0.000396
        assert abs(a_today["cost"] - 0.000396) < 1e-9
        assert (
            a_today["pricing_source"]
            == "flat tariff table (not metered upstream usage)"
        )

        b_today = by[("2026-10-21", "…2222")]
        assert b_today["requests"] == 1
        assert b_today["input_tokens"] == 7
        assert b_today["output_tokens"] == 3

        a_older = by[("2026-10-19", "…1111")]
        assert a_older["requests"] == 1
        assert a_older["input_tokens"] == 10

        # masked aliases only: no raw key material anywhere in the output
        blob = str(rows)
        assert "dsk-aaaa1111" not in blob
        assert "dsk-bbbb2222" not in blob
    finally:
        functions._db = saved


def test_retries_count_once_and_failures_record_nothing():
    """A turn that fails twice upstream and succeeds on the third token must
    record exactly ONE request; a turn that never succeeds records ZERO."""
    saved = _fresh_store()
    calls = {"send": 0}

    async def fake_sig(messages, model, scope=""):
        return "sig-usage-retry"

    def fake_pick(*a, **k):
        return 1

    def fake_get_token(tid):
        return {"id": tid, "token": "tok", "status": "ACTIVE"}

    async def fake_files(messages, token, last_user_only=False):
        return []

    async def fake_prompt(messages, tools, model, is_first, rollover_summary=None):
        return "prompt"

    def fake_send(
        chat_id,
        auth_token,
        message,
        parent,
        thinking=False,
        search=False,
        file_ids_=None,
    ):
        calls["send"] += 1
        if calls["send"] < 3:

            async def fail_gen():
                raise RuntimeError(
                    "Empty response from DeepSeek (no parseable SSE content)"
                )
                yield ""  # pragma: no cover

            return fail_gen()

        async def ok_gen():
            yield "final answer"

        return ok_gen()

    async def scenario():
        app_module._chat_locks.clear()

        patches = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),
            ("pick_token", fake_pick),
            ("get_token", fake_get_token),
            ("send_message", fake_send),
            ("create_new_chat", lambda tok: _achat("chat-usage")),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a: None),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved_patches = [(name, getattr(app_module, name)) for name, _ in patches]
        for name, fn in patches:
            setattr(app_module, name, fn)
        try:
            return await app_module.handle_chat(
                [{"role": "user", "content": "hi"}], "v4.1flash", scope="dsk-retrykey"
            )
        finally:
            for name, fn in saved_patches:
                setattr(app_module, name, fn)
            app_module._chat_locks.clear()

    try:
        result = asyncio.run(scenario())
        assert result == "final answer"
        rows = functions.usage_by_day_key()
        assert len(rows) == 1, rows
        assert rows[0]["requests"] == 1, "streamed retries must count once"
        assert rows[0]["output_tokens"] > 0, "tokens billed on the cleaned reply"
        assert "dsk-retrykey" not in str(rows), "key material must be masked"

        # a turn that never succeeds records nothing
        def always_fail(*a, **k):
            # sync on purpose: send_message is an async GENERATOR function —
            # callers do `gen = send_message(...)` (no await) and iterate.
            # An `async def` fake here returns a coroutine instead of an
            # async generator, so the iterate site explodes with TypeError
            # and abandons an un-awaited coroutine (RuntimeWarning noise;
            # review fix, PR #58-#67 test-hygiene).
            async def fail_gen():
                raise RuntimeError(
                    "Empty response from DeepSeek (no parseable SSE content)"
                )
                yield ""  # pragma: no cover

            return fail_gen()

        patches2 = [
            ("get_auth_token", lambda: "tok"),
            ("generate_signature", fake_sig),
            ("find_session", lambda s: None),
            ("pick_token", lambda *a, **k: 1),
            ("get_token", fake_get_token),
            ("send_message", always_fail),
            ("create_new_chat", lambda tok: _achat("chat-usage-2")),
            ("extract_and_upload_files", fake_files),
            ("build_prompt", fake_prompt),
            ("mark_limited", lambda tid: None),
            ("mark_active", lambda tid: None),
            ("delete_sessions_for_chat", lambda *a: None),
            ("save_session", lambda *a: None),
            ("record_file", lambda *a: None),
            ("parse_tools", lambda t: ([], t)),
            ("format_response", lambda text, model, messages, tools=None: text),
        ]
        saved2 = [(name, getattr(app_module, name)) for name, _ in patches2]
        for name, fn in patches2:
            setattr(app_module, name, fn)
        try:
            app_module._chat_locks.clear()
            resp = asyncio.run(
                app_module.handle_chat(
                    [{"role": "user", "content": "hi again"}],
                    "v4.1flash",
                    scope="dsk-retrykey",
                )
            )
            assert getattr(resp, "status_code", None) == 502, resp
        finally:
            for name, fn in saved2:
                setattr(app_module, name, fn)
            app_module._chat_locks.clear()
        rows2 = functions.usage_by_day_key()
        assert sum(r["requests"] for r in rows2) == 1, (
            "failed turns must record zero usage"
        )
    finally:
        functions._db = saved


def test_empty_store_renders_honest_nothing():
    saved = _fresh_store()
    try:
        assert functions.usage_by_day_key() == [], (
            "an empty store is an empty table, never an error"
        )
    finally:
        functions._db = saved


async def _achat(session_id):
    return session_id


TESTS = [
    test_every_cell_of_the_expected_table,
    test_retries_count_once_and_failures_record_nothing,
    test_empty_store_renders_honest_nothing,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            import traceback

            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
