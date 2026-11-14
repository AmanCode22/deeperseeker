# Changelog

All notable user-visible changes. One bullet per change, each citing its PR
or issue — no citation, no bullet. Dates are the day the change landed on
main.

## [Unreleased]

### Fixed

- First message of a chat no longer sends `parent_message_id` (upstream began rejecting `0` with biz_code 26 "invalid message id" on 2026-10-31, and edges disagree on whether `null` is accepted). Follow-up turns keep the `parent = previous response id` chain. (PR #92)
- An upstream failure delivered as `application/json` on the SSE endpoint (as that biz_code 26 was) now names its code and message in the raised error and logs the response content-type with the raw non-SSE body, instead of surfacing as an anonymous empty stream. (PR #92)
- Upstream business errors (HTTP 200 with `code=40003 INVALID_TOKEN`, "messages too frequent", a killed web session) surface as a typed 401/429 and rotate the token instead of raising `TypeError` inside the route and pinning the dead account forever. The pre-turn create hops (`create_new_chat`, `create_challange_pow`) go through the same classifier, outside the per-signature lock so the retry cannot deadlock. (PR #92)
- Upstream hang-ups mid-stream log a warning and emit the terminal error frame instead of a traceback and a dead socket. (PR #92)
- A single upstream turn is bounded by `DEEPSEEKER_REQUEST_DEADLINE` (default 600 s); a slowly-dripping upstream no longer holds a request until the client gives up. (PR #92)
- The pool no longer hands out an account that is still cooling down. When every account is cooling, the request is refused with 429 and a `Retry-After` instead of being sent straight back into the same refusal — that fallback is the first half of the chain issue #105 traced from "messages too frequent" to a revoked account (40003). `DEEPSEEKER_REFUSE_WHEN_ALL_COOLING=0` restores the old behaviour. (#105)
- A 429 now rests the account for longer with each consecutive refusal — 60 s, 300 s, 900 s, 3600 s — instead of a flat 60 s cooldown, and a successful turn resets the streak, so one bad minute no longer condemns an account. `DEEPSEEKER_RATE_LIMIT_LADDER` sets the ladder; after `DEEPSEEKER_RATE_LIMIT_PARK_AFTER` consecutive 429s the account is parked for an hour instead of cooled again. (#105)
- Every rate refusal now answers with the shape of the surface it was sent to: the Anthropic routes return `{"type": "error", …}`, the OpenAI routes `{"error": {…}}`. The 429 body of the token-rotation path carried the OpenAI shape on the Anthropic routes, so a Claude Code client could not read it. (#105)
- The token-rotation path — an existing conversation whose account is cooling and no other account is free — answers with 429 and a `Retry-After` like every other rate path, instead of a bare 429 that carried no wait time at all. (#105)
- A conversation that moved to another account is refused against that account's budget, not against the budget of the account the session started on. (#105)
- The deep-health probe tells "every account is cooling down or over its request budget" apart from "there is no account in the pool", and names the wait time in the first case. (#105)
- The rolling per-minute window is read from worker threads and written from the event loop, so its reads, writes and pruning now run under one lock; an unguarded read-then-pop could empty the window between the check and the pop and turn a burst into a 500. (#105)
- The file endpoints (`POST /v1/files`, `POST /v1/files/upload`, `GET /v1/files/{id}/content`) obey the same per-minute budget as a chat turn: they spend it, and they are refused with 429 and a `Retry-After` when the account is already over it. (#105)
- The 429 and 403 streak counters increment and report in one statement, so the cooldown rung and the parking threshold can no longer be decided by another caller's increment. (#105)

### Added

- `POST /v1/messages/count_tokens` (also at `/messages/count_tokens`) — Claude Code's context probes returned 404 before. (PR #92)
- Every account can have a rolling per-minute request budget (`DEEPSEEKER_TOKEN_MAX_MSGS_PER_MIN`). It is **opt-in** — unset keeps today's unbounded behaviour, and `5` is the value issue #105 suggests for agent workloads, where one user turn is 5–15 upstream messages. When the budget is on, a request that would exceed it is refused with 429 and a `Retry-After` before it reaches upstream, and the scheduler prefers an account that still has room in its window. `DEEPSEEKER_TOKEN_MAX_WAIT` bounds how long a request waits for the window to slide instead of being refused. (#105)
- Upstream `Retry-After` values now reach the client instead of being consumed internally, and the dashboard shows each account's budget usage and 429 streak next to its in-flight count. (#105)

## [6.0] — 2026-10-21

### Fixed

- Streaming: one completion id/created per OpenAI stream, strict chunk fields (`index`, `model`) — Vercel AI SDK clients (Trilium, Zed) reject frames without them. (PR #38)
- Context rollover: system prompt and tools are restored when a conversation rolls over into a fresh chat, so handoffs keep the runtime contract. (PR #39)
- Upstream SSE errors are parsed and surfaced with their real status instead of being misreported as context-limit failures. (PR #35)
- Usage chunk is emitted in OpenAI streaming responses so gateways (New API, sub2api) bill correctly. (PR #31)
- Context rollover keeps continuity across sessions; conversation history and `previous_response_id` state survive. (PR #34, #35)
- DSML bare markers and plural `function_calls` container markers no longer leak into replies. (PR #36)
- Streaming chunks carry a per-chunk index. (PR #36)
- Token pool: least-in-flight selection with idle-oldest tie-break under a soft per-token concurrency cap; rate-limited tokens recover by cooldown instead of starving. Root cause of #32. (B3, Stage 1 audit)
- Client aborts purge the stored session rows so the next turn cannot fork the conversation. (B2)
- Rollover decisions are serialized under the per-chat lock; concurrent same-signature requests can no longer double-roll. (B1)
- Blocking SQLite helpers run off the event loop — no more head-of-line stalls under concurrent streams. (B5)
- Token billing uses the cleaned completion text (thinking + tool markup are not billed output). (B7)
- Empty-SSE retries rotate tokens with jittered backoff and a bounded budget instead of re-rolling the same poisoned session. (B10, #33)
- Files are pinned to their token; foreign references are re-homed to the chat's account before sending. (B4)
- Every blocking terminal empty-SSE now returns a designed OpenAI/Anthropic 502 with a trace id (`error.request_id` / `[trace: …]`) and a redacted body. (PR, P0-4, closes #33)
- A stale in-flight success can no longer lift a quarantine. (P0-3)

### Added

- Token pool lifecycle: 401 quarantines a token immediately; repeated 403s escalate to quarantine (`DEEPSEEKER_QUARANTINE_AFTER_403`); nothing leaves quarantine except Restore on the dashboard. Pool state view on the dashboard with masked aliases. (PR, P0-3, closes #32)
- Security defaults: an unset API key is generated, printed once and persisted; default admin credentials meeting a non-loopback bind refuse to start, naming the fix (`DEEPSEEKER_ALLOW_DEFAULT_ADMIN=1` is the explicit override). (B9 + PR, P0-8)
- The container runs as an unprivileged user (uid 10001); only `/app/data` is writable. (PR, P0-8)
- Multi-arch release images on every `v*` tag: `ghcr.io/amancode22/deeperseeker` (amd64 + arm64), tags `<version>` / `<major>` / `latest`, digest printed in the run log; image HEALTHCHECK + startup identity line (version + build commit). (PR, P0-2)
- Deep health probe: `GET /health?deep=1` (auth required) performs one real upstream round trip including PoW and reports reachability + latency; the shallow check stays public. (PR, P0-9)
- Usage stats on the dashboard: requests, input/output tokens and estimated cost per day × per masked key for the last 30 days; streamed retries count once; cost labeled as a flat-tariff estimate. (PR, P0-6)
- Official-API passthrough mode: `DEEPSEEKER_MODE=official` + `DEEPSEEKER_OFFICIAL_API_KEY` serves chat/messages/models from the official API — the exit door when the bridge breaks. Explicit mode, no silent fallback. (PR, P0-7)
- README: risk banner, no-public-relays stance, takedown policy, status table backed by the deep probe, client compatibility matrix, Chinese quickstart. (PR, P0-9)
- CHANGELOG, issue templates (bug / Ban report / feature / deploy-help), SECURITY.md, CONTRIBUTING.md. (PR, P0-10)

### Changed

- Supply chain: uv + `requirements*.in` lockfile flow, ruff enforcement, CI pipeline, dependabot. (PR #40 and deps PRs)
- Docker entrypoint fixed: `CMD ["deeperseeker"]` now resolves (`/app/.venv/bin` on PATH) — the image was not bootable as-is before. (PR, P0-8)
