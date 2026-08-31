# DeeperSeeker

DeepSeek website reverse-proxy server with FastAPI, supporting OpenAI & Anthropic API standards.

If you want to use deeperseeker with claude desktop app see [Claude Desktop Setup Guide](CLAUDE_DESKTOP_SETUP.md)

For solving deepseek pow challenge the wasm file included is from my other repo: https://github.com/AmanCode22/deepseek_pow_solver/

If you liked this repo please star it and star the solver also.

⚠️ Warning: Automated use violates DeepSeek's Terms ofUse.
Use a dedicated throwaway account, never your personal one.
Accounts may be banned at any time. 
Use at your own risk.

A kind request: do not spam the server, respect DeepSeek's limits, and use it for personal purposes only.


## Quickstart

### Local Python
```bash
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python3 app.py
```

> **Note:** Cookie generation launches Chromium with `headless=False` (DeepSeek blocks headless browsers). On a server without a display, run through `xvfb`:
> ```bash
> xvfb-run -a -s '-screen 0 1280x720x24' python3 app.py
> ```

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

The container runs Chromium under `xvfb-run` automatically. Note: `docker-compose.yml` binds to `127.0.0.1:4000` only (local access) — change the ports mapping if you need to expose it.

Dashboard: `http://localhost:4000/`

## Configuration (`.env`)

Copy `.env.example` to `.env` and edit your secret values:

```bash
cp .env.example .env
```

| Variable | Description | Default |
|---|---|---|
| `DEEPSEEKER_API_KEY` | Bearer API key required to access endpoints | `dseeker` |
| `DEEPSEEKER_ADMIN_USER` | Dashboard login username | `admin` |
| `DEEPSEEKER_ADMIN_PASSWORD` | Dashboard login password | `admin` |
| `HOST` | Bind address for bare-metal runs (`127.0.0.1` = local only, `0.0.0.0` = expose) | `127.0.0.1` |
| `PORT` | Server port | `4000` |

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
- **Models**: `instant` (flash), `vision` (flash + vision), `expert` (pro) — also exposed as `anthropic/claude-instant`, `anthropic/claude-vision`, `anthropic/claude-expert` aliases for Claude Desktop auto-discovery. If no `model` is sent, requests default to `expert`.

## Features

- **Multi-Token Pooling**: Random active token rotation.
- **Context-Based Session Selector**: Computes a SHA-256 signature over the canonicalized message history (up to the last assistant turn), the model, and the API key scope, to match and resume existing web chat sessions. Session creation is lock-protected to avoid duplicates.
- **Full History Injection**: Inject full conversation history into new sessions when session signature is not in DB or when account fails over.
- **Automatic Rate-Limit Recovery**: Auto-marks tokens `RATE_LIMITED` on HTTP 401/403/429, provisions a new token, transfers full context (including files), and continues seamless chat with a single retry.
- **Tool Calling & Streaming**: Server-sent events (SSE) streaming with think-tag reassembly across chunk boundaries (no truncation) and multi-format tool-call parsing — DSML XML, `<tool_call>` XML, `<function_call>` blocks, and JSON — into OpenAI/Anthropic tool schemas.
- **File & Vision Support**: Base64/URL image extraction (with SSRF protection), file upload streaming, and vision-model file forking.
- **Claude Desktop Compatible**: Rich `/v1/models` capability metadata + `anthropic/claude-*` aliases for automatic client discovery.
- **Hardened Dashboard**: Session TTL, brute-force login lockout (5 attempts → 5 min), CSRF origin check.

## Pricing (per 1M tokens, as of 2026-08-30)

| Model Tier | Time Window | Input Cost (Cache Miss) | Output Cost |
|---|---|---|---|
| **DeepSeek V4 Flash** (fast, lightweight tasks) | Off-Peak Hours | $0.22 | $0.44 |
| | Peak Hours | $0.66 | $1.32 |
| **DeepSeek V4 Pro** (flagship, coding, reasoning) | Off-Peak Hours | $0.66 | $1.32 |
| | Peak Hours | $1.32 | $1.98 |

Model mapping: `instant`/`vision` → V4 Flash, `expert` → V4 Pro. The `cost` reported in API responses uses the flat Peak Hour rates.
## Star History

<a href="https://www.star-history.com/?repos=amancode22%2Fdeeperseeker&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&theme=dark&legend=top-left&sealed_token=p51g9NMqgaq9Vca1q98lgnVZqL_FE7o7RLnrOq13r0inPOUkYbEE3LBTpI8D_4EkYuRvL_Ml_PBqRbO3z1hE_zUPPEan4-PMNLSBlic-1JWthx8oVIgbQlFve_lO8yAn2o-BZRIlGtxpWQr1ejz6_cJJVNCcx48izzlLcZcsQalxAg3DO-HVPukVbYlj" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left&sealed_token=p51g9NMqgaq9Vca1q98lgnVZqL_FE7o7RLnrOq13r0inPOUkYbEE3LBTpI8D_4EkYuRvL_Ml_PBqRbO3z1hE_zUPPEan4-PMNLSBlic-1JWthx8oVIgbQlFve_lO8yAn2o-BZRIlGtxpWQr1ejz6_cJJVNCcx48izzlLcZcsQalxAg3DO-HVPukVbYlj" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=amancode22/deeperseeker&type=date&legend=top-left&sealed_token=p51g9NMqgaq9Vca1q98lgnVZqL_FE7o7RLnrOq13r0inPOUkYbEE3LBTpI8D_4EkYuRvL_Ml_PBqRbO3z1hE_zUPPEan4-PMNLSBlic-1JWthx8oVIgbQlFve_lO8yAn2o-BZRIlGtxpWQr1ejz6_cJJVNCcx48izzlLcZcsQalxAg3DO-HVPukVbYlj" />
 </picture>
</a>

## Disclaimer

Educational purpose only. This project is not affiliated with, endorsed by, or sponsored by DeepSeek. Use responsibly and in accordance with DeepSeek's terms of service.

