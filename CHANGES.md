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

## 5. FOLLOW-UP BUG — Server freezes after many chats / cookies vanish across restarts (fixed)

Reported: after enough chats the server stopped responding to **any** request
(even brand-new chats), and after a mid-session restart the DeepSeek cookie
file was missing from the data volume and never regenerated.

Root causes found (all fixed):

**a) Cookie file silently escaped the persistent volume** (`functions.py` +
`Dockerfile`): the Docker image bridges `aws_cookies_deepseek.json` into
`/app/data` via a **symlink**, but `_generate_cookies()` ends with
`os.replace(tmp, "aws_cookies_deepseek.json")` — POSIX `rename()` does **not**
follow symlinks, so the very first cookie write replaced the *symlink itself*
with a plain file in the ephemeral container layer. The volume never received
the cookie; any container recreation lost it permanently (SQLite writes
*through* the symlink, which is why `deeperseeker.db` survived).
→ Cookie path is now resolved (`cookie_file_path()`: `DEEPSEEKER_COOKIE_PATH`
env > symlink target > directory of the real DB file) and the atomic
`os.replace()` targets the **resolved** path, so the symlink is preserved.
`Dockerfile` additionally sets `DB_PATH` / `DEEPSEEKER_COOKIE_PATH` to point
straight into `/app/data`, making the symlinks unnecessary.

**b) One failed cookie generation froze the whole server** (`functions.py`):
`get_cookies()` serialized **every** request on `_cookie_lock` and each queued
request re-attempted a full non-headless Chromium launch; when generation kept
failing (missing/expired WAF token, display problems), requests queued
indefinitely — the "server stops responding" symptom.
→ `get_cookies()` now: double-checks under the lock; bounds each generation
cycle (`DEEPSEEKER_COOKIE_TIMEOUT`, default 120s) and attempts
(`DEEPSEEKER_COOKIE_ATTEMPTS`, default 2); arms a fail-fast cooldown
(`DEEPSEEKER_COOKIE_COOLDOWN`, default 20s) so further requests error in
milliseconds instead of queueing; falls back to a **stale** cookie file rather
than hard-failing; raises a clear `CookieGenerationError` (mapped to HTTP 503
by `_api_error_response`) when nothing is available.

**c) Zero observability** (`functions.py` / `app.py`): cookie generation,
rate-limit events and upstream failures failed silently, so nothing appeared
in `docker logs`.
→ Added `deeperseeker.functions` logging (generation start/success/failure
with cause, cookie save path + expiry, token rate-limiting, session pruning,
upstream non-200 responses, recovery failures) and `basicConfig` in `app.py`.

**d) `_sig_locks` grew forever** (`app.py`): one `asyncio.Lock` per unique
conversation signature, never evicted — a real memory leak after many chats.
→ Capped at `DEEPSEEKER_MAX_SIG_LOCKS` (default 4096) with locked-entry-aware
eviction of the oldest entries.

**e) Sessions table grew forever** (`functions.py`): 2 rows stored per request
with no cleanup; on volume-limited deployments a full disk also freezes all
requests. → `prune_sessions()` keeps the newest `DEEPSEEKER_MAX_SESSIONS`
(default 20000) rows, runs every `DEEPSEEKER_PRUNE_EVERY` (500) saves and once
at startup; stale `session_map` rows older than 7 days are removed too.

**f) `get_session()` double-init race** (`functions.py`): two concurrent
first requests could create two `aiohttp.ClientSession`s (one leaked).
→ Now guarded by a lock and re-checked for closed sessions.

Net effect: cookies persist across restarts/recreations, a failing cookie
regeneration degrades to fast, well-described 503s (and auto-retries) instead
of a full-server freeze, and memory/DB growth is bounded.

## 6. Verification (follow-up round)

- `py_compile` clean on all modified files.
- `tests/test_local_fixes.py` 5/5 (PR #11 regression suite).
- 15/15 integration sanity checks (previous fix round).
- 16/16 stability checks: symlink-safe cookie write (symlink survives atomic
  replace, file lands in the volume), regeneration on missing file, fail-fast
  cooldown with root-cause message, stale-cookie fallback, shared-session
  double-init race, lock-cap eviction (held locks preserved), session pruning
  to cap with oldest-first eviction.
