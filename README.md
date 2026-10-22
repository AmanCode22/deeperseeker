# DeeperSeeker

[![CI Pipeline](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml/badge.svg)](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml)
[![Release image](https://github.com/AmanCode22/deeperseeker/actions/workflows/release.yml/badge.svg)](https://github.com/AmanCode22/deeperseeker/actions/workflows/release.yml)

A self-hosted reverse proxy that turns a DeepSeek web session into an **OpenAI- and Anthropic-compatible API** — FastAPI underneath, with streaming, tool calling, files and vision.

**English** · [简体中文](README.zh-CN.md)

## Table of contents

- [What is this?](#what-is-this)
- [Warnings — read before you run anything](#warnings--read-before-you-run-anything)
- [Status](#status)
- [Quickstart](#quickstart)
  - [Prebuilt image (recommended)](#option-1--prebuilt-image-ghcrio-amd64--arm64--recommended)
  - [Docker Compose](#option-2--docker-compose)
  - [Local Python with uv](#option-3--local-python-with-uv-recommended-for-development)
  - [Local Python without uv](#option-4--local-python-without-uv)
- [Auth token setup](#auth-token-setup)
- [Configuration (`.env`)](#configuration-env)
- [API endpoints & usage](#api-endpoints--usage)
- [Official-API mode (the exit door)](#official-api-mode-the-exit-door)
- [Features](#features)
- [Security defaults](#security-defaults)
- [Troubleshooting](#troubleshooting)
- [Pricing](#pricing)
- [Project docs](#project-docs)
- [Star history](#star-history)
- [Contributors](#contributors)
- [Disclaimer](#disclaimer)

## What is this?

DeeperSeeker runs on your machine, holds one or more of your own DeepSeek web
session tokens, and exposes standard API endpoints (`/v1/chat/completions`,
`/v1/responses`, `/v1/messages`) that talk to the DeepSeek web backend for
you. Any client that speaks OpenAI or Anthropic — Claude Desktop, agent
harnesses, scripts — can point at it and work, with a dashboard for the token
pool and usage stats. The single default model is `v4.1flash`.

For solving the DeepSeek PoW challenge the wasm file included is from my
other repo: <https://github.com/AmanCode22/deepseek_pow_solver/>. If you
liked this repo please star it and star the solver also.

Want to use DeeperSeeker with the Claude Desktop app? Follow the
[Claude Desktop Setup Guide](CLAUDE_DESKTOP_SETUP.md).

## Warnings — read before you run anything

- This project drives a **reverse-engineered web interface**, not a sanctioned API. Automated use violates DeepSeek's Terms of Use.
- **Account-ban risk is real and on you**: use a dedicated throwaway account, never your personal one. Accounts may be banned at any time, without warning.
- Personal use only. Run it for yourself, on your own account, at your own risk.

**No public relays, ever.** This is a self-hosted tool for one person's own account. Operating a shared/public relay on top of it concentrates exactly the abuse that gets accounts banned and projects killed (see the ds2api story) — it will not be supported, and issues asking for help running one will be closed.

**Takedown / contact policy.** I'm Aman (AmanCode22, amanady125@gmail.com). If you are DeepSeek and this project harms you: email me and I will respond — taking the repo down or removing specific components is on the table, and notice is honored quickly. Bug reports and security reports follow [SECURITY.md](SECURITY.md).

## Status

| Check | State | Last verified | Notes |
|---|---|---|---|
| Upstream health (bridge mode) | see `/health?deep=1` | run it yourself | the deep probe does one real PoW + chat-session round trip; a table row written at release time is stale the moment it ships, so the source of truth is the authenticated probe |
| PoW (DeepSeekHashV1) | working via [deepseek_pow_solver](https://github.com/AmanCode22/deepseek_pow_solver/) wasm | 2026-10 | if DeepSeek raises the difficulty, the solver repo gets the fix first |
| OpenAI surface (`/v1/chat/completions`, `/v1/responses`) | working | 2026-10 | streaming + tools; see the client matrix below |
| Anthropic surface (`/v1/messages`) | working | 2026-10 | Claude Desktop guide in CLAUDE_DESKTOP_SETUP.md |
| Official-API mode | see [Official-API mode](#official-api-mode-the-exit-door) | 2026-10 | the exit door when the bridge dies |

A status table you stop checking is a table that lies — treat "last verified" as the claim's expiry date.

Per-client compatibility lives in [docs/CLIENT_MATRIX.md](docs/CLIENT_MATRIX.md): one row per real, dated run — no run, no row.

## Quickstart

Four ways to run, all reading the same `.env`. First, no matter which you pick:

```bash
cp .env.example .env
```

Set at minimum `DEEPSEEKER_ADMIN_PASSWORD` in that file — [Security defaults](#security-defaults) refuses exposed binds with default credentials.

### Option 1 — Prebuilt image (ghcr.io, amd64 + arm64) — recommended

Images are **built only by CI** (`.github/workflows/release.yml`) — never locally, never by hand. The version has one source: `pyproject.toml` (`version = "6.0"` today). A release is: bump that field, push the matching `v<version>` tag (here `v6.0`) — the workflow refuses to build if the tag and `pyproject.toml` disagree, then publishes a multi-arch (amd64 + arm64) image. Pull-and-run, no repo clone needed:

```bash
export DEEPSEEKER_ADMIN_PASSWORD='pick-a-real-password'   # P0-8 refuses admin/admin on exposed binds
docker run -d --name deeperseeker -p 4000:4000 \
  -e DEEPSEEKER_ADMIN_USER=admin -e DEEPSEEKER_ADMIN_PASSWORD \
  ghcr.io/amancode22/deeperseeker:latest
```

Prefer pinning the digest you verified over a moving tag:

```bash
docker pull ghcr.io/amancode22/deeperseeker:6.0 \
  && docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/amancode22/deeperseeker:6.0
# then run ghcr.io/amancode22/deeperseeker@sha256:<digest-of-that-output>
```

Tags: the full version from `pyproject.toml` (e.g. `6.0`), the major line (e.g. `6`), and `latest` — all three always come from that one field, so an image tag can never disagree with the package version. The same directory works with `podman pull`/`podman run` unchanged.

### Option 2 — Docker Compose
Suggested in issue https://github.com/AmanCode22/deeperseeker/issues/3; works with podman-compose too.

Compose ships with ```build: .``` so you can run from source — but to run a released version, point it at the prebuilt image: add one  line under ```build: .``` in docker-compose.yml:

```    image: ghcr.io/amancode22/deeperseeker:6.0   # run the release; build: stays as a fallback```

With both keys present, docker compose pull fetches the release image and build: is only used if you explicitly pass --build.

```bash

cp .env.example .env          # set DEEPSEEKER_ADMIN_PASSWORD (compose refuses to boot without it)
docker compose pull           # released multi-arch image (amd64 + arm64)
docker compose up -d
docker compose logs --since 1m deeperseeker   # identity line: version + build commit
Verify, then leave it running:
```
```bash

curl http://127.0.0.1:4000/health
curl -H "Authorization: Bearer <DEEPSEEKER_API_KEY>" 'http://127.0.0.1:4000/health?deep=1'  # real upstream round trip incl. PoW
```

- State lives in the named volume deeperseeker_data (/app/data) — survives restarts and upgrades; never docker compose down -v.
- Upgrade = bump the image: tag (e.g. 6.0 → 7.0), then docker compose pull && docker compose up -d.
- From source instead: remove the image: line (or docker compose up -d --build) to build locally.
- The compose file binds 127.0.0.1:4000 only — change the ports mapping if you must expose it, and set real credentials first (see Security defaults).

### Option 3 — Local Python with uv (recommended for development)

Install [uv](https://docs.astral.sh/uv/) first:

```bash
# linux / macOS
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# windows
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```bash
uv sync
source .venv/bin/activate   # or activate.fish / activate.zsh
cp .env.example .env
uv run deeperseeker
```

### Option 4 — Local Python without uv

```bash
python3 -m venv .venv
pip install -r requirements.txt
cp .env.example .env
python3 app.py
```

> **Note:** thanks to PR <https://github.com/AmanCode22/deeperseeker/pull/16> by [@alan7383](https://github.com/alan7383), Playwright and Chromium are deprecated and kept as backup (DeepSeek does not enforce AWS WAF on requests using Android client headers). You no longer need to run `playwright install chromium` or `xvfb-run` unless reverting to the backup cookie mechanism — and it saves RAM and is much more stable than cookie harvesting.

## Auth token setup

1. Open an incognito window → `chat.deepseek.com` → log in.
2. Console (F12): `JSON.parse(localStorage.getItem("userToken")).value`
3. Paste the raw token string into the dashboard (`/dashboard`). Close the incognito window.

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit your secret values. The table below is the full knob set; sane defaults mean you normally set only the admin pair.

| Variable | Description | Default |
|---|---|---|
| `DEEPSEEKER_API_KEY` | Bearer API key required to access endpoints; if unset, a strong key is generated on first boot, printed once and persisted to `api_key.txt` | generated |
| `DEEPSEEKER_ADMIN_USER` | Dashboard login username | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | Dashboard login password — defaults + non-loopback bind refuse to boot (see Security defaults) | `admin` |
| `DEEPSEEKER_ALLOW_DEFAULT_ADMIN` | Explicitly accept admin/admin on a non-loopback bind (not recommended); only the literal value `1` unlocks the boot | unset |
| `HOST` | Bind address for bare-metal runs (`127.0.0.1` = local only, `0.0.0.0` = expose) | `127.0.0.1` |
| `PORT` | Server port | `4000` |
| `DEEPSEEKER_MAX_HISTORY_TOKENS` | Token cap for injected history on first-message prompt builds | `24000` |
| `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS` | Token cap for injected tool results | `12000` |
| `DEEPSEEKER_MEMORY_LIMIT_TOKENS` | Observed remembered-context limit that triggers rollover | `393228` |
| `DEEPSEEKER_FIRST_MESSAGE_TOKENS` | Observed single-first-message acceptance (informational) | `974848` |
| `DEEPSEEKER_MAX_OUTPUT_TOKENS` | Observed per-response output cap (reported by `/v1/models`) | `8192` |
| `DEEPSEEKER_ROLLOVER_SAFETY_TOKENS` | Headroom reserved below the memory limit before summarizing | `24000` |
| `DEEPSEEKER_MAX_SUMMARY_TOKENS` | Budget for the model-generated handoff summary | `4096` |
| `DEEPSEEKER_PER_TOOL_RESULT_TOKENS` | Per-result tool-output cap (so one giant output can't eat the budget) | `2000` |
| `DEEPSEEKER_RATE_LIMIT_COOLDOWN` | Seconds a rate-limited token cools before it rejoins the pool automatically | `60` |
| `DEEPSEEKER_TOKEN_CONCURRENCY` | Soft per-token in-flight cap; at-cap tokens are used only when nothing else is free | `8` |
| `DEEPSEEKER_QUARANTINE_AFTER_403` | Consecutive 403s that take a token out of rotation until you restore it (`0` disables escalation) | `3` |
| `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` | Retries of one turn across the pool before the upstream error is surfaced to the client | `3` |
| `DEEPSEEKER_MAX_USER_MESSAGE_TOKENS` | Hard cap for the latest user message inside a prompt; above it the message is tail-truncated with a marker instead of failing upstream | `900000` |
| `DEEPSEEKER_PROMPT_OVERHEAD_TOKENS` | Flat overhead added to the rollover estimate for tool schemas, system expansion and summaries the raw history does not show | `4096` |
| `DEEPSEEKER_MODE` | `bridge` (reverse-engineered web session) or `official` (official API passthrough). Explicit only — nothing falls back on its own | `bridge` |
| `DEEPSEEKER_OFFICIAL_API_KEY` | Official API key used when mode is `official` (from platform.deepseek.com). Required in official mode | unset |
| `DEEPSEEKER_OFFICIAL_BASE` | Official API base URL override in official mode | `https://api.deepseek.com` |

## API endpoints & usage

- **OpenAI Base**: `http://localhost:4000/v1`
  - `POST /v1/chat/completions` (streaming & non-streaming)
  - `POST /v1/responses` (Responses API)
  - `GET /v1/models`
  - `POST /v1/files`
  - `GET /v1/files/{file_id}`
  - `GET /v1/files/{file_id}/content`
- **Anthropic Base**: `http://localhost:4000`
  - `POST /v1/messages` (also at `/messages`)
  - `POST /v1/files/upload`
- **Auth Key**: configured in `.env` (`DEEPSEEKER_API_KEY`)

### Model

`v4.1flash` — the single default DeepSeek model (vision-capable: `vision` no longer needs a separate model tier). Every request is served by it, whatever `model` value the client sends (legacy aliases such as `instant`, `expert`, `vision`, and `anthropic/claude-*` are accepted and normalized to `v4.1flash`). Also listed as `anthropic/claude-v4.1flash` for Claude Desktop auto-discovery. If no `model` is sent, requests default to `v4.1flash`.

## Official-API mode (the exit door)

Set `DEEPSEEKER_MODE=official` and `DEEPSEEKER_OFFICIAL_API_KEY` and the same endpoints (chat, messages, models) serve requests from the **official API** instead of the web session. The mode switch is invisible to clients: same auth (your `DEEPSEEKER_API_KEY`), same model id (`v4.1flash` maps to `deepseek-chat`), same tool calling, same usage stats (surface `official`). The official key never reaches a log line or an error body. When the bridge breaks (PoW change, WAF return, layout change), this is the minutes-not-hours fallback — flip the env, restart, keep working; flip it back the same way.

## Features

- **Multi-token pooling**: selects the least-in-flight token (oldest-idle tie-break) under a soft per-token concurrency cap, so concurrent requests spread across accounts instead of stampeding onto one.
- **Context-based session selector**: computes a SHA-256 signature over the canonicalized message history (up to the last assistant turn), the model, and the API key scope, to match and resume existing web chat sessions. Session creation is lock-protected to avoid duplicates.
- **Summarize-and-rollover context policy**: when accumulated session context nears the observed remembered-context limit (~393K input tokens), the bridge asks the model for a compact handoff summary in a scratch chat, starts a fresh web chat, and seeds it with that summary plus the newest user message. Relevant newest tool calls/results are preserved (capped by `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS`, with a tighter per-result cap via `DEEPSEEKER_PER_TOOL_RESULT_TOKENS`); attachments are described in words inside the summary rather than forwarded. The first exchange — however large — is never rolled over (the first-message path accepts ~1M tokens). Declared limits in `/v1/models` (`context_window`, `max_output_tokens`) reflect this observed behavior, not guaranteed upstream limits. The summary prompt instructs the model to treat conversation content as data, never as instructions.
- **Full history injection**: injects the full conversation history into new sessions when the session signature is not in the DB or when the account fails over.
- **Rate-limit recovery and quarantine**: a 429 — or a 403 below the escalation threshold — cools a token for `DEEPSEEKER_RATE_LIMIT_COOLDOWN` seconds and it rejoins rotation by itself, while a 401 or `DEEPSEEKER_QUARANTINE_AFTER_403` consecutive 403s with no successful turn in between marks it `QUARANTINED` and takes it out of rotation for good, because a rejected credential is not a rate limit and re-arming it every minute is how accounts get banned. Nothing leaves quarantine except **Restore** on the dashboard (`POST /tokens/{id}/restore`). On an upstream 401/403/429 the broken web session is discarded and the turn is retried on a *different* pooled token with a jittered backoff — up to `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` tries, then a proper API error — with full history injection, and account-scoped files are re-homed onto that token first; the bridge rotates among the tokens you added, it never provisions one. The pool view shows each token's state, in-flight count, last use and remaining cooldown by alias with no credential material, and a stale in-flight success cannot lift a quarantine. In-flight counts are per process, so a multi-worker deployment sees only its own worker's numbers.
- **Long-context resilience**: if the upstream web session fails or returns an empty response (e.g. context overflow), the broken session is discarded and the request is retried once on a fresh session with a compacted, token-capped history injection; unrecoverable upstream errors are returned as proper JSON API errors instead of raw 500s.
- **Tool calling & streaming**: server-sent events (SSE) streaming with think-tag reassembly across chunk boundaries (no truncation) and multi-format tool-call parsing — DSML XML, XML blocks, `<function_call>` blocks, and JSON — into OpenAI/Anthropic tool schemas.
- **File & vision support**: base64/URL image extraction (with SSRF protection) and document upload streaming. Images and documents are referenced directly via `ref_file_ids` — the vision file-forking step (`fork_file_task`) was removed because `v4.1flash` handles vision natively.
- **Usage stats**: the dashboard shows requests, input/output tokens and an estimated cost per day × per masked key alias for the last 30 days — "which client burned my account?", answered honestly. One row per *completed* turn: failed attempts and streamed retries never double-count, tokens are billed on the cleaned reply (the same numbers the API usage block reports), and cost is labeled as a flat-tariff estimate, not upstream metering. The read-time aggregation stays a few milliseconds at a month of data (indexed `GROUP BY`, measured: ~30k rows → ~10 ms).
- **Claude Desktop compatible**: rich `/v1/models` capability metadata + the `anthropic/claude-v4.1flash` alias for automatic client discovery.
- **Hardened dashboard**: session TTL, brute-force login lockout (5 attempts → 5 min), CSRF origin check.

## Security defaults

What each boot does, verified by actually running it (P0-8 before/after matrix):

- **No `DEEPSEEKER_API_KEY` set** (Boot 1): a strong key is generated, printed once on boot, and persisted to `api_key.txt` next to the database so restarts keep the same key. Never an open relay.
- **Default dashboard credentials + loopback bind** (Boot 2): boots with a loud warning suggesting `DEEPSEEKER_ADMIN_USER` / `DEEPSEEKER_ADMIN_PASSWORD`. Local development stays zero-config.
- **Default dashboard credentials + non-loopback bind** (Boot 3): **refuses to start**, naming the fix — set the credential pair, stay loopback, or set `DEEPSEEKER_ALLOW_DEFAULT_ADMIN=1` to accept the risk explicitly (not recommended). Anyone who can reach the dashboard owns the token pool; that is the exposed-relay configuration that killed ds2api, so it dies at boot time now, not in a postmortem.

The container runs as an unprivileged user (uid 10001); only `/app/data` (database, cookie backup, generated key) is writable. A **new** named volume inherits that ownership automatically. A volume populated by a pre-P0-8 root-era image holds root-owned files — hand it over once, then use the image normally (and the reverse direction is always safe: a chown'd volume also works under a root-era image):

```bash
docker run --rm -u root -v deeperseeker_deeperseeker_data:/app/data alpine \
  chown -R 10001:10001 /app/data
```

## Troubleshooting

- **Empty or blank replies, then an HTTP 502** — *Symptom:* the client gets `Empty response from DeepSeek (no parseable SSE content)` (or an upstream error event) and finally a `502` with an `error.request_id` (OpenAI surface) or a `[trace: …]` suffix in the message (Anthropic surface). *What the service does now:* the turn is retried across the pool — each attempt on a different token, jittered backoff between attempts — up to `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` tries; when every attempt comes back empty, the designed 502 is returned with the correlation id stamped in the body and the `X-Request-ID` header, and nothing upstream-derived reaches the body unredacted. *What to check:* open the dashboard pool view and look for tokens stuck `RATE_LIMITED`/`QUARANTINED`; match the `request_id`/`trace` against the access log line to see which states the turn traversed.
- **"Context limit" errors in an agent harness** (Claude Code, Cline, coding harnesses) — *Symptom:* the harness reports a context-limit failure while plain chat clients keep working. *Root cause:* the upstream context belongs to the **chat session**, not the token — the old plain retry re-sent the same oversized context (a harness replays its whole history every turn) and every attempt failed the same way. *What the service does now:* (1) a context-class failure (upstream context wording, or the empty-SSE-with-context-hint error) makes the retry **force a summarize-and-rollover** — the fresh chat is seeded with a model-generated handoff summary, so the turn recovers with full continuity instead of dying or amputating the history; (2) the latest user message is hard-capped (`DEEPSEEKER_MAX_USER_MESSAGE_TOKENS`, tail kept) so a first message above the ~975K upstream ceiling passes instead of failing on every attempt; (3) the rollover estimate counts tool schemas and prompt overhead (`DEEPSEEKER_PROMPT_OVERHEAD_TOKENS`) so the proactive rollover fires **before** the remembered session dies, not after.

## Pricing (per 1M tokens, as of 2026-09-06)

Flat peak-hour rates for the single default model:

| Model Tier | Input Cost (Cache Miss) | Output Cost |
|---|---|---|
| **DeepSeek V4.1 Flash** (`v4.1flash`) | $0.44 | $1.32 |

The `cost` reported in API responses uses these flat rates.

## Project docs

- [Claude Desktop Setup Guide](CLAUDE_DESKTOP_SETUP.md) — point Claude Desktop at DeeperSeeker in a few minutes.
- [docs/CLIENT_MATRIX.md](docs/CLIENT_MATRIX.md) — which clients were tested, when, with what result.
- [SECURITY.md](SECURITY.md) — how to report security issues.
- [CONTRIBUTING.md](CONTRIBUTING.md) — branch/test/merge ritual for patches.
- [CHANGELOG.md](CHANGELOG.md) — what changed, release by release.

## Star history

<a href="https://www.star-history.com/?repos=amancode22%2Fdeeperseeker&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left" />
 </picture>
</a>

## Contributors

[![Contributors](https://contrib.rocks/image?repo=amancode22/deeperseeker)](https://github.com/amancode22/deeperseeker/graphs/contributors)

## Disclaimer

Educational purpose only. This project is not affiliated with, endorsed by, or sponsored by DeepSeek. Use responsibly and in accordance with DeepSeek's terms of service.
