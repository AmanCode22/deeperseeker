# DeeperSeeker

DeepSeek website reverse-proxy server with FastAPI, supporting OpenAI & Anthropic API standards.

## Quickstart

```bash
python3 -m venv deeperseeker_env
source deeperseeker_env/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python3 app.py
```

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

## Auth Token Setup

1. Open incognito window -> `chat.deepseek.com` -> Login
2. Console (F12): `JSON.parse(localStorage.getItem("userToken")).value`
3. Paste raw token string into Dashboard (`/dashboard`). Close incognito window.

## API Endpoints & Usage

- **OpenAI Base**: `http://localhost:4000/v1`
  - `POST /v1/chat/completions` (streaming & non-streaming)
  - `GET /v1/models`
  - `POST /v1/files`
  - `GET /v1/files/{file_id}`
- **Anthropic Base**: `http://localhost:4000`
  - `POST /v1/messages`
  - `POST /v1/files/upload`
- **Auth Key**: Configured in `.env` (`DEEPSEEKER_API_KEY`)
- **Models**: `instant` (flash), `vision` (flash + vision), `expert` (pro)

## Features

- **Multi-Token Pooling**: Random active token rotation.
- **Context-Based Session Selector**: Computes SHA-256 context signature (`system + first_user + first_assistant`) to match and resume existing web chat sessions.
- **Full History Injection**: Inject full conversation history into new sessions when session signature is not in DB or when account fails over.
- **Automatic Rate-Limit Recovery**: Auto-marks tokens `RATE_LIMITED` on HTTP 429/errors, provisions a new token, transfers full context, and continues seamless chat.
- **Tool Calling & Streaming**: Server-sent events (SSE) streaming and XML tool-call parser into OpenAI/Anthropic tool schemas.
- **File & Vision Support**: Base64/URL image extraction and file upload streaming.

## Pricing (per 1M tokens)

| Model | Cache Miss Input | Cache Hit Input | Output |
|-------|------------------|-----------------|--------|
| `deepseek-v4-flash` (`instant`/`vision`) | $0.14 | $0.0028 | $0.28 |
| `deepseek-v4-pro` (`expert`) | $0.435 | $0.003625 | $0.87 |
