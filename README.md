# DeeperSeeker
[![CI Pipeline](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml/badge.svg)](https://github.com/AmanCode22/deeperseeker/actions/workflows/tests.yaml)

DeepSeek website reverse-proxy server with FastAPI, supporting OpenAI & Anthropic API standards.

[English](#deeperseeker) · [中文快速开始](#中文快速开始)

If you want to use deeperseeker with claude desktop app see [Claude Desktop Setup Guide](CLAUDE_DESKTOP_SETUP.md)

For solving deepseek pow challenge the wasm file included is from my other repo: https://github.com/AmanCode22/deepseek_pow_solver/

If you liked this repo please star it and star the solver also.

⚠️ **Risk banner** — read before you run anything:

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
| Official-API mode (P0-7) | see README section below | 2026-10 | the exit door when the bridge dies |

A status table you stop checking is a table that lies — treat "last verified" as the claim's expiry date.

Per-client compatibility lives in [docs/CLIENT_MATRIX.md](docs/CLIENT_MATRIX.md): one row per real, dated run — no run, no row.

## Quickstart

### Local Python[UV, Recommended]
Needs uv first

for linux/macos
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
or on windows
```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```
Then,
```bash
uv sync
source .venv/bin/activate # or activate.fish or activate.zsh
cp .env.example .env
uv run deeperseeker
```
### Local Python[Non UV, Not usually recommended]
```bash
python3 -m venv .venv
pip install -r requirements.txt 
cp .env.example .env 
python3 app.py
```


> **Note:** Thanks to PR https://github.com/AmanCode22/deeperseeker/pull/16 by [@alan7383](https://github.com/alan7383), Playwright and Chromium are deprecated and kept as backup (DeepSeek does not enforce AWS WAF on requests using Android client headers). You no longer need to run `playwright install chromium` or `xvfb-run` unless reverting to the backup cookie mechanism. And it now saves ram also and is much stable than cookie harvesting.

### Docker / Podman (Podman recommended for rootless execution)

Suggested in issue [#3](https://github.com/AmanCode22/deeperseeker/issues/3)

```bash
cp .env.example .env

# Using Podman (Recommended - Rootless)
podman build -t deeperseeker .
podman run -d --name deeperseeker -p 4000:4000 --env-file .env deeperseeker

# Using Docker
docker build -t deeperseeker .
docker run -d --name deeperseeker -p 4000:4000 --env-file .env deeperseeker

# Or using Podman Compose / Docker Compose
podman-compose up -d
# docker compose up -d
```

Note: `docker-compose.yml` binds to `127.0.0.1:4000` only (local access) — change the ports mapping if you need to expose it.

Dashboard: `http://localhost:4000/`

### Prebuilt image (ghcr.io, amd64 + arm64)

Multi-arch images publish on every `v*` tag. Pull-and-run, no repo clone needed:

```bash
export DEEPSEEKER_ADMIN_PASSWORD='pick-a-real-password'   # P0-8 refuses admin/admin on exposed binds
docker run -d --name deeperseeker -p 4000:4000 \
  -e DEEPSEEKER_ADMIN_USER=admin -e DEEPSEEKER_ADMIN_PASSWORD \
  ghcr.io/amancode22/deeperseeker:latest
```

Prefer pinning the digest you verified over a moving tag:

```bash
docker pull ghcr.io/amancode22/deeperseeker:v1 \
  && docker inspect --format '{{index .RepoDigests 0}}' ghcr.io/amancode22/deeperseeker:v1
# then run ghcr.io/amancode22/deeperseeker@sha256:<digest-of-that-output>
```

Tags: `<full version>` (e.g. `v1.0.0`), `<major>` (e.g. `v1`), and `latest`. The same directory works with `podman pull`/`podman run` unchanged. Config-heavy setups: keep using the compose file above (`cp .env.example .env`, fill in the admin pair, `docker compose up -d`) — the image reads the same environment variables.

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit your secret values:

```bash
cp .env.example .env
```

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

## Auth Token Setup

1. Open incognito window -> `chat.deepseek.com` -> Login
2. Console (F12): `JSON.parse(localStorage.getItem("userToken")).value`
3. Paste raw token string into Dashboard (`/dashboard`). Close incognito window.

## API Endpoints & Usage

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
- **Auth Key**: Configured in `.env` (`DEEPSEEKER_API_KEY`)
- **Model**: `v4.1flash` — the single default DeepSeek model (vision-capable: `vision` no longer needs a separate model tier). Every request is served by it, whatever `model` value the client sends (legacy aliases such as `instant`, `expert`, `vision`, and `anthropic/claude-*` are accepted and normalized to `v4.1flash`). Also listed as `anthropic/claude-v4.1flash` for Claude Desktop auto-discovery. If no `model` is sent, requests default to `v4.1flash`.

## Features

- **Multi-Token Pooling**: Selects the least-in-flight token (oldest-idle tie-break) under a soft per-token concurrency cap, so concurrent requests spread across accounts instead of stampeding onto one.
- **Context-Based Session Selector**: Computes a SHA-256 signature over the canonicalized message history (up to the last assistant turn), the model, and the API key scope, to match and resume existing web chat sessions. Session creation is lock-protected to avoid duplicates.
- **Summarize-and-Rollover Context Policy**: When accumulated session context nears the observed remembered-context limit (~393K input tokens), the bridge asks the model for a compact handoff summary in a scratch chat, starts a fresh web chat, and seeds it with that summary plus the newest user message. Relevant newest tool calls/results are preserved (capped by `DEEPSEEKER_MAX_TOOL_RESULT_TOKENS`, with a tighter per-result cap via `DEEPSEEKER_PER_TOOL_RESULT_TOKENS`); attachments are described in words inside the summary rather than forwarded. The first exchange — however large — is never rolled over (the first-message path accepts ~1M tokens). Declared limits in `/v1/models` (`context_window`, `max_output_tokens`) reflect this observed behavior, not guaranteed upstream limits. The summary prompt instructs the model to treat conversation content as data, never as instructions.
- **Full History Injection**: Inject full conversation history into new sessions when session signature is not in DB or when account fails over.
- **Rate-Limit Recovery and Quarantine**: A 429 — or a 403 below the escalation threshold — cools a token for `DEEPSEEKER_RATE_LIMIT_COOLDOWN` seconds and it rejoins rotation by itself, while a 401 or `DEEPSEEKER_QUARANTINE_AFTER_403` consecutive 403s with no successful turn in between marks it `QUARANTINED` and takes it out of rotation for good, because a rejected credential is not a rate limit and re-arming it every minute is how accounts get banned. Nothing leaves quarantine except **Restore** on the dashboard (`POST /tokens/{id}/restore`). On an upstream 401/403/429 the broken web session is discarded and the turn is retried on a *different* pooled token with a jittered backoff — up to `DEEPSEEKER_MAX_UPSTREAM_ATTEMPTS` tries, then a proper API error — with full history injection, and account-scoped files are re-homed onto that token first; the bridge rotates among the tokens you added, it never provisions one. The pool view shows each token's state, in-flight count, last use and remaining cooldown by alias with no credential material, and a stale in-flight success cannot lift a quarantine. In-flight counts are per process, so a multi-worker deployment sees only its own worker's numbers.
- **Long-Context Resilience**: If the upstream web session fails or returns an empty response (e.g. context overflow), the broken session is discarded and the request is retried once on a fresh session with a compacted, token-capped history injection; unrecoverable upstream errors are returned as proper JSON API errors instead of raw 500s.
- **Tool Calling & Streaming**: Server-sent events (SSE) streaming with think-tag reassembly across chunk boundaries (no truncation) and multi-format tool-call parsing — DSML XML, `<tool_call>` XML, `<function_call>` blocks, and JSON — into OpenAI/Anthropic tool schemas.
- **File & Vision Support**: Base64/URL image extraction (with SSRF protection) and document upload streaming. Images and documents are referenced directly via `ref_file_ids` — the vision file-forking step (`fork_file_task`) was removed because `v4.1flash` handles vision natively.
- **Claude Desktop Compatible**: Rich `/v1/models` capability metadata + the `anthropic/claude-v4.1flash` alias for automatic client discovery.
- **Hardened Dashboard**: Session TTL, brute-force login lockout (5 attempts → 5 min), CSRF origin check.

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

## Pricing (per 1M tokens, as of 2026-09-06)

Flat peak-hour rates for the single default model:

| Model Tier | Input Cost (Cache Miss) | Output Cost |
|---|---|---|
| **DeepSeek V4.1 Flash** (`v4.1flash`) | $0.44 | $1.32 |

The `cost` reported in API responses uses these flat rates.
## Star History

<a href="https://www.star-history.com/?repos=amancode22%2Fdeeperseeker&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left" />
 </picture>
</a>
## Disclaimer

Educational purpose only. This project is not affiliated with, endorsed by, or sponsored by DeepSeek. Use responsibly and in accordance with DeepSeek's terms of service.

## 中文快速开始

**这是什么**：把 DeepSeek 网页版反向代理成 OpenAI / Anthropic 兼容接口的自托管服务。⚠️ 风险提示：自动化使用违反 DeepSeek 服务条款，账号可能随时被封——请只用小号，自用即可，风险自负。**禁止搭建公共转发站。**

**安装（推荐 uv）：**

```bash
git clone https://github.com/AmanCode22/deeperseeker && cd deeperseeker
uv sync
source .venv/bin/activate
cp .env.example .env        # 填写 DEEPSEEKER_ADMIN_PASSWORD 等配置
uv run deeperseeker
```

**首次启动**：若未设置 `DEEPSEEKER_API_KEY`，服务会自动生成一个强密钥并打印一次（同时保存在数据库旁的 `api_key.txt`）。调用接口时带上 `Authorization: Bearer <该密钥>`。

**添加令牌**：浏览器无痕窗口登录 `chat.deepseek.com` → 控制台（F12）执行 `JSON.parse(localStorage.getItem("userToken")).value` → 复制返回的令牌 → 粘贴到 `http://localhost:4000/dashboard`（默认账号密码 `admin`/`admin`，仅限本机访问时使用；对外暴露前必须修改，否则服务会拒绝启动）。

**调用接口**：

```bash
curl http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer <你的密钥>" \
  -H "Content-Type: application/json" \
  -d '{"model": "v4.1flash", "messages": [{"role": "user", "content": "你好"}]}'
```

**检查服务是否还活着**：`curl -H "Authorization: Bearer <你的密钥>" "http://localhost:4000/health?deep=1"` —— 深度探活会真实走一次上游（含 PoW），返回 `upstream_reachable` 与延迟。更多配置见上方英文配置表。

## Contributors

[![Contributors](https://contrib.rocks/image?repo=amancode22/deeperseeker)](https://github.com/amancode22/deeperseeker/graphs/contributors)
