"""P0-7: official DeepSeek API passthrough (the exit door).

acheong08-style deaths happen when a project has exactly one upstream and no
way out. This module is the way out: when DEEPSEEKER_MODE=official, the same
public handlers (chat, messages, models) call api.deepseek.com with a real
official API key instead of the reverse-engineered web session. Nothing
client-visible changes: same auth (the bridge's own DEEPSEEKER_API_KEY), same
logging, same usage recording — only the upstream differs.

v1 scope, explicit:
  * explicit mode only — DEEPSEEKER_MODE=official; no silent auto-fallback
  * chat surface: full support (stream + non-stream, tools in native OpenAI
    format — no DSML translation needed upstream)
  * messages surface: supported by translating Anthropic requests (already
    OpenAI-shaped inside the bridge) and re-shaping the official reply; a
    streaming request is answered with correct Anthropic SSE built from the
    complete upstream answer (v1 keeps one upstream call shape)
  * models surface: official ids with documented limits
  * the official key never reaches logs or error bodies — errors raise
    UpstreamError with the upstream STATUS and a redacted body slice
"""

import json
import logging
import os
import time
import uuid

import aiohttp

from functions import UpstreamError, get_session

logger = logging.getLogger("uvicorn.error")

OFFICIAL_BASE = (
    os.getenv("DEEPSEEKER_OFFICIAL_BASE") or "https://api.deepseek.com"
).rstrip("/")
OFFICIAL_API_KEY = (os.getenv("DEEPSEEKER_OFFICIAL_API_KEY") or "").strip()


def official_mode_enabled():
    """True only when the operator set DEEPSEEKER_MODE=official explicitly.

    Anything else (unset, 'bridge', a typo) stays in bridge mode — the exit
    door must be opened on purpose, never fallen into."""
    return (os.getenv("DEEPSEEKER_MODE") or "bridge").strip().lower() == "official"


# Bridge model id -> official model id. The bridge exposes one model
# (v4.1flash); official mode maps it to deepseek-chat. Clients keep sending
# whatever they sent before — the mode switch is invisible to them.
# deepseek-chat and deepseek-reasoner are official ids themselves (reasoner
# is advertised by /v1/models below): the unknown-id default must not
# silently reroute a requested reasoner to the chat model (PR #64 review
# fix, Medium).
MODEL_MAP = {
    "v4.1flash": "deepseek-chat",
    "deepseek-chat": "deepseek-chat",
    "deepseek-reasoner": "deepseek-reasoner",
}


def map_model(model):
    return MODEL_MAP.get(str(model or "").strip(), "deepseek-chat")


def _headers():
    return {
        "Authorization": f"Bearer {OFFICIAL_API_KEY}",
        "Content-Type": "application/json",
    }


def _redact(text):
    """Upstream body slices may carry echo'd request material; key material is
    never logged. Truncate + drop anything Bearer-shaped."""
    import re

    scrubbed = re.sub(r"(?i)bearer\s+[A-Za-z0-9._+=/-]+", "[redacted]", str(text))
    scrubbed = re.sub(r"[A-Za-z0-9+/_=-]{32,}", "[redacted]", scrubbed)
    return scrubbed[:300]


async def _post_json(path, payload):
    """One official API POST. Raises UpstreamError(status, redacted body) on
    any non-200 — callers treat it exactly like a bridge UpstreamError."""
    if not OFFICIAL_API_KEY:
        raise UpstreamError(
            503, "official mode is on but DEEPSEEKER_OFFICIAL_API_KEY is not set"
        )
    session = await get_session()
    try:
        async with session.post(
            f"{OFFICIAL_BASE}{path}",
            headers=_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        ) as resp:
            body = await resp.text()
            if resp.status != 200:
                logger.warning("official API %s answered HTTP %s", path, resp.status)
                raise UpstreamError(resp.status, _redact(body))
            return json.loads(body)
    except aiohttp.ClientError as e:
        raise UpstreamError(502, f"official API unreachable: {type(e).__name__}") from e


def _strict_chunk(chunk, cid, created, model):
    """PR #38 contract: one completion = one id/created, every chunk carries
    id/object/created/model and choices[].index. Official chunks are already
    OpenAI-shaped; this enforces the strict fields so the same envelope tests
    pass in both modes."""
    out = {
        "id": cid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [],
    }
    for i, ch in enumerate(chunk.get("choices") or []):
        out["choices"].append(
            {
                "index": ch.get("index", i),
                "delta": ch.get("delta") or {},
                "finish_reason": ch.get("finish_reason"),
            }
        )
    if chunk.get("usage"):
        out["usage"] = chunk["usage"]
    return out


async def official_chat(payload, stream=False):
    """Call the official /chat/completions.

    Non-stream: returns the official chat.completion dict as-is (it already
    IS the OpenAI shape the bridge's format_response targets).

    Stream: yields 'data: {json}\n\n' SSE lines normalized to the strict
    chunk shape above, with a trailing usage chunk and [DONE] — byte-for-byte
    the same envelope contract stream_response() produces in bridge mode.

    Split into a coroutine and an async generator: a function that both
    returns a value and yields is not expressible in Python, and silently
    turning the non-stream result into StopIteration.value would be exactly
    the kind of bug this project keeps paying for."""
    if not stream:
        payload = dict(payload)
        payload["model"] = map_model(payload.get("model"))
        return await _post_json("/chat/completions", payload)
    return _official_chat_stream(payload)


async def _official_chat_stream(payload):
    payload = dict(payload)
    payload["model"] = map_model(payload.get("model"))
    if not OFFICIAL_API_KEY:
        raise UpstreamError(
            503, "official mode is on but DEEPSEEKER_OFFICIAL_API_KEY is not set"
        )
    session = await get_session()  # ensure the shared client session exists
    cid = "chatcmpl-" + uuid.uuid4().hex
    created = int(time.time())
    client_model = payload.get("model")
    usage_seen = None
    try:
        # PR #64 review fix (HIGH): this must be an ABSOLUTE official URL.
        # The old code handed f"{OFFICIAL_BASE}/chat/completions" to
        # post_with_failover(path) — which prepends the BRIDGE base
        # (session.post(base + path)) — producing
        # https://chat.deepseek.comhttps://api.deepseek.com/chat/completions,
        # a URL that can never resolve: every official-mode streaming chat
        # died 502/DNS, and CI missed it because the envelope test mocked
        # official_chat wholesale. _post_json already posted the absolute
        # URL; the streaming path now does exactly the same.
        resp = await session.post(
            f"{OFFICIAL_BASE}/chat/completions",
            headers=_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=300),
        )
    except (TimeoutError, aiohttp.ClientError) as e:
        raise UpstreamError(502, f"official API unreachable: {type(e).__name__}") from e
    async with resp:
        if resp.status != 200:
            body = (await resp.text()) or ""
            raise UpstreamError(resp.status, _redact(body))
        async for raw_line in resp.content:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:") :].strip()
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                continue
            usage_seen = chunk.get("usage") or usage_seen
            yield f"data: {json.dumps(_strict_chunk(chunk, cid, created, client_model))}\n\n"
    if usage_seen:
        tail = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": client_model,
            "choices": [],
            "usage": usage_seen,
        }
        yield f"data: {json.dumps(tail)}\n\n"
    yield "data: [DONE]\n\n"


OFFICIAL_MODELS = [
    {
        "id": "deepseek-chat",
        "object": "model",
        "type": "model",
        "name": "deepseek-chat",
        "display_name": "DeepSeek Chat (official API)",
        "owned_by": "deepseek",
        # Documented official limits, not observations — cite api-docs.
        "context_window": 131072,
        "max_output_tokens": 8192,
        "capabilities": {"image_input": {"supported": False}},
    },
    {
        "id": "deepseek-reasoner",
        "object": "model",
        "type": "model",
        "name": "deepseek-reasoner",
        "display_name": "DeepSeek Reasoner (official API)",
        "owned_by": "deepseek",
        "context_window": 131072,
        "max_output_tokens": 8192,
        "capabilities": {"image_input": {"supported": False}},
    },
]
