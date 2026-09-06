# deeperseeker — Change Summary

**Date:** 2026-09-06 · **Scope:** 5 files changed, 138 insertions(+), 32 deletions(-)

Files touched: `app.py`, `functions.py`, `plugin_helper.py`, `Dockerfile`, `README.md`

> **Integration note:** the original fix was produced against an older snapshot
> (base `73d42fc`). It has been rebased onto current `main`, preserving the
> changes merged in PRs #11–#13: `convert_anthropic_messages()` (the
> `content.strip()` isinstance guard from this fix is already superseded there),
> the exact-match signature lookup (the prefix-fallback loop this fix hardened
> no longer exists), and the `next_parent()` helper (kept alongside the new
> `delete_sessions_for_chat()`). `py_compile` clean; `tests/test_local_fixes.py`
> 5/5 pass; 15/15 integration sanity checks pass.

---

## 1. THE MAIN BUG — Long context kills the agent (fixed)

Root causes found (all fixed):

**a) Empty upstream responses were silently returned** (`functions.py`)
When the prompt exceeded the DeepSeek web session's context limit, the upstream
stream ended with `FINISHED` and **zero content**. `send_message()` returned an
empty completion — the agent "shuts off" with no error.
→ `send_message()` now tracks `got_output` and raises a clear exception
("Empty response from DeepSeek (prompt may exceed the session context limit)")
on FINISHED-with-no-content, empty `v` strings, and silent stream ends.

**b) Streaming had zero error protection** (`app.py`)
For streamed requests (what agents use), every upstream error — HTTP 400
context overflow, 401, 429 — surfaced only *after* `StreamingResponse` headers
were already sent, so retry logic never engaged and the client just saw a dead
stream.
→ New `_preflight_stream()` consumes the first chunk eagerly before headers
are sent; upstream errors now surface *before* streaming starts and can
trigger recovery. `_replay_stream()` replays the pre-read chunk to the client.

**c) No recovery path for a dead/broken session** (`app.py`)
Only ONE DB session row was deleted on failure, and the prefix-signature
search immediately resurrected the same broken/rate-limited session — the
conversation was permanently dead.
→ `handle_chat()` now calls the new `delete_sessions_for_chat()` (wipes ALL
rows bound to the broken chat) and retries ONCE on a completely fresh session
with full history re-injected (guarded by `_retried` to prevent loops).
Recovery now triggers on ANY upstream failure, not just 401/403/429.

**d) Rebuilt prompts themselves overflowed** (`plugin_helper.py`)
When rebuilding a session, the entire conversation history + all tool results
were injected verbatim into one prompt — overflowing DeepSeek's context window
again, in a loop.
→ Injected history is now capped at 24k tokens and tool results at 12k tokens
(env-tunable: `DEEPSEEKER_MAX_HISTORY_TOKENS` /
`DEEPSEEKER_MAX_TOOL_RESULT_TOKENS`). Oldest parts are dropped first, newest
always kept, with explicit `[... truncated ...]` markers.
→ `role="tool"` messages are no longer duplicated into
`[PREVIOUS CONVERSATION HISTORY]` (they already live in `[TOOL RESULTS]`).

**e) Session prefix-match false positives** (`app.py`)
The prefix-signature fallback search could match a stale session mid-history.
→ Search now only extends across assistant-message boundaries.

Net effect: long conversations now auto-recover — broken session discarded →
fresh chat with compacted, token-capped history → single clean retry.

## 2. Other bugs found & fixed

- **Raw 500s on upstream errors** (`app.py`): added `_api_error_response()` —
  proper OpenAI/Anthropic-shaped JSON errors with correct HTTP status codes
  instead of plain-text 500s agents cannot parse.
- **All-tokens-rate-limited fallthrough** (`app.py`): returned a clean `429`
  JSON error instead of falling through and sending anyway / crashing.
- **`j["file_data"]` KeyError** (`plugin_helper.py`): was reading the wrong
  nesting level → fixed to `j["file"]["file_data"]`, plus a missing-filename
  guard (`file.bin` fallback) in `extract_and_upload_files()`.
- **`content.strip()` crash** (`app.py`): Anthropic handler crashed on
  assistant messages whose `content` is a list (non-string) — added an
  `isinstance` guard.
- **`build_prompt()` / file upload failures bypassed recovery** (`app.py`):
  moved both inside the `try` block so their failures also trigger the
  fresh-session retry.
- **Security default** (`app.py` + `Dockerfile`): HOST default changed
  `0.0.0.0` → `127.0.0.1`; `ENV HOST=0.0.0.0` added to the Dockerfile so
  containers still bind externally.

## 3. Vision pricing → DeepSeek V4 Flash Exp

- `functions.py` `DEEPSEEK_TARIFFS`: new `deepseek-v4-flash-exp` entry
  ($0.44 in / $1.32 out per 1M tokens, peak), mapped to the `vision` model in
  `format_response()` cost calculation.
- Corrected to official peak rates (verified 2026-09-06,
  api-docs.deepseek.com): Flash input $0.66 → **$0.44**; Pro output
  $1.98 → **$3.96**.
- README pricing table now **flat peak-hour only** — all off-peak rows
  removed — with the new **DeepSeek V4 Flash Exp (`vision`)** row.

## 4. Verification

- `py_compile` clean on all modified files.
- 34/34 automated checks pass: session chain & cleanup, signature round-trips,
  prompt dedup/capping, tariff values, preflight/replay streaming, empty
  response detection, mocked `handle_chat` recovery + 429 path, tool-parser
  regressions.
