# Changelog

All notable user-visible changes. One bullet per change, each citing its PR
or issue — no citation, no bullet. Dates are the day the change landed on
main.

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
