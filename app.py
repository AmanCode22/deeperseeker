import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import deepseek_tokenizer
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

load_dotenv()

API_KEY = os.getenv("DEEPSEEKER_API_KEY", "dseeker")
ADMIN_USER = os.getenv("DEEPSEEKER_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.getenv("DEEPSEEKER_ADMIN_PASSWORD", "admin")

security = HTTPBasic()


from functions import (
    add_token,
    count_tokens,
    create_new_chat,
    delete_token,
    find_session,
    get_auth_token,
    get_token,
    get_tokens,
    init_db,
    mark_limited,
    mark_active,
    next_parent,
    parse_tools,
    pick_token,
    save_session,
    delete_session,
    send_message,
    StreamToolParser,
    upload_file,
    get_file_content,
)
from plugin_helper import build_prompt, extract_and_upload_files, generate_signature, generate_signature_sync


logger = logging.getLogger("uvicorn.error")


def count_tok(text):
    return len(deepseek_tokenizer.ds_token.encode(text))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="DeeperSeeker", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length", "")
    if cl.isdigit() and int(cl) > 32 * 1024 * 1024:
        return JSONResponse({"error": "Request body too large"}, status_code=413)
    return await call_next(request)


SESSIONS = {}
SESSION_TTL = 7 * 24 * 3600
_sig_locks = {}
_login_fails = {"count": 0, "locked_until": 0}


def get_current_admin(request: Request):
    sid = request.cookies.get("session_id")
    if not sid or sid not in SESSIONS or time.time() - SESSIONS[sid] > SESSION_TTL:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    SESSIONS[sid] = time.time()
    origin = request.headers.get("origin", "")
    if origin:
        parsed = urlparse(origin).netloc
        if parsed and parsed != request.headers.get("host", ""):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return "admin"


def get_api_key(request: Request):
    auth = request.headers.get("authorization", "")
    api_key_header = request.headers.get("x-api-key", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    elif auth:
        return auth
    return api_key_header


def check_key(request: Request):
    key = get_api_key(request)
    return secrets.compare_digest(key.encode("utf-8"), API_KEY.encode("utf-8"))


async def handle_chat(messages, model, thinking=False, search=False, stream=False, tools=None, is_anthropic=False, req_model=None, scope="", _retried=False):
    auth_token = get_auth_token()
    if not auth_token:
        return JSONResponse({"error": "No auth token. Add via dashboard."}, status_code=401)

    sig = await generate_signature(messages, model, scope)
    sess = find_session(sig)
    if not sess:
        for i in range(len(messages) - 1, 0, -1):
            sess = find_session(generate_signature_sync(messages[:i], model, scope))
            if sess:
                break

    if sess:

        token_id = sess["token_id"]
        session_id = sess["session_id"]
        parent_message_id = sess["parent_message_id"]
        tok = get_token(token_id)
        if not tok or tok["status"] == "RATE_LIMITED":
            new_token_id = pick_token()
            if new_token_id and (not tok or new_token_id != token_id):
                new_tok = get_token(new_token_id)
                if new_tok:
                    new_session_id = await create_new_chat(new_tok["token"])
                    prompt = await build_prompt(messages, tools or [], model, is_first_message=True)

                    file_ids = await extract_and_upload_files(messages, new_tok["token"])
                    gen = send_message(new_session_id, new_tok["token"], prompt, 0, thinking, search, None if model == "instant" else model, file_ids)
                    if stream:
                        if is_anthropic:
                            return StreamingResponse(stream_anthropic_response(gen, model, messages, new_token_id, new_session_id, sig, tools, req_model, 0, scope), media_type="text/event-stream")
                        return StreamingResponse(stream_response(gen, model, messages, new_token_id, new_session_id, sig, tools, 0, scope), media_type="text/event-stream")
                    else:
                        resp_text = await collect_response(gen)
                        mark_active(new_token_id)

                        parsed_tools, clean_text = parse_tools(resp_text)
                        clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
                        clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()
                        next_messages = messages.copy()
                        ast_msg = {"role": "assistant"}
                        if parsed_tools:
                            ast_msg["tool_calls"] = parsed_tools
                        else:
                            ast_msg["content"] = clean_text
                        next_messages.append(ast_msg)
                        next_sig = await generate_signature(next_messages, model, scope)

                        save_session(sig, new_token_id, new_session_id, next_parent(0))
                        save_session(next_sig, new_token_id, new_session_id, next_parent(0))
                        return format_response(resp_text, model, messages, tools)
    else:

        create_lock = _sig_locks.setdefault(sig, asyncio.Lock())
        async with create_lock:
            sess = find_session(sig)
            if not sess:
                token_id = pick_token()
                if not token_id:
                    return JSONResponse({"error": "No tokens available"}, status_code=503)
                tok = get_token(token_id)
                if not tok:
                    return JSONResponse({"error": "Token not found"}, status_code=503)
                session_id = await create_new_chat(tok["token"])
                save_session(sig, token_id, session_id, 0)
                parent_message_id = 0
            else:
                token_id = sess["token_id"]
                session_id = sess["session_id"]
                parent_message_id = sess["parent_message_id"]

    tok = get_token(token_id)
    if not tok:
        return JSONResponse({"error": "Token expired"}, status_code=503)

    is_first = parent_message_id == 0
    file_ids = await extract_and_upload_files(messages, tok["token"], last_user_only=not is_first)
    prompt = await build_prompt(messages, tools or [], model, is_first)

    try:
        gen = send_message(session_id, tok["token"], prompt, parent_message_id, thinking, search, None if model == "instant" else model, file_ids)
        if stream:
            if is_anthropic:
                return StreamingResponse(stream_anthropic_response(gen, model, messages, token_id, session_id, sig, tools, req_model, parent_message_id, scope), media_type="text/event-stream")
            return StreamingResponse(stream_response(gen, model, messages, token_id, session_id, sig, tools, parent_message_id, scope), media_type="text/event-stream")
        else:
            resp_text = await collect_response(gen)
            mark_active(token_id)

            parsed_tools, clean_text = parse_tools(resp_text)
            clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
            clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()
            next_messages = messages.copy()
            ast_msg = {"role": "assistant"}
            if parsed_tools:
                ast_msg["tool_calls"] = parsed_tools
            else:
                ast_msg["content"] = clean_text
            next_messages.append(ast_msg)
            next_sig = await generate_signature(next_messages, model, scope)

            save_session(sig, token_id, session_id, next_parent(parent_message_id))
            save_session(next_sig, token_id, session_id, next_parent(parent_message_id))
            return format_response(resp_text, model, messages, tools)
    except Exception as e:
        delete_session(sig)
        m = re.match(r"HTTP (\d{3}):", str(e))
        code = int(m.group(1)) if m else None
        if code in (401, 403, 429):
            mark_limited(token_id)
        if _retried or code not in (401, 403, 429):
            raise
        return await handle_chat(messages, model, thinking, search, stream, tools, is_anthropic, req_model, scope, _retried=True)


async def collect_response(gen):
    text = ""
    async for chunk in gen:
        text += chunk
    return text


def _messages_text(messages):
    parts = []
    for m in messages:
        c = m.get("content", "")
        if isinstance(c, list):
            parts.append(" ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"))
        else:
            parts.append(str(c))
    return "\n".join(parts)


async def _hold_think_tags(gen):
    carry = ""
    async for chunk in gen:
        chunk = carry + chunk
        carry = ""
        hold = 0
        for tag in ("<think>", "</think>"):
            for i in range(1, len(tag)):
                if chunk.endswith(tag[:i]):
                    hold = max(hold, i)
        if hold:
            carry = chunk[-hold:]
            chunk = chunk[:-hold]
        if chunk:
            yield chunk
    if carry:
        yield carry


async def stream_response(gen, model, messages, token_id, session_id, sig, tools, parent_message_id=0, scope=""):
    parser = StreamToolParser()
    full_text = ""
    is_thinking = False
    aborted = False
    failed = False
    try:
        async for chunk in _hold_think_tags(gen):
            if not chunk:
                continue
            full_text += chunk

            if "<think>" in chunk:
                is_thinking = True
                chunk = chunk.replace("<think>", "").lstrip("\n")

            end_thinking = False
            if "</think>" in chunk:
                is_thinking = False
                end_thinking = True
                parts = chunk.split("</think>")
                think_part = parts[0]
                chunk = parts[1].lstrip("\n") if len(parts) > 1 else ""
                if think_part:
                    yield f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': think_part}}]})}\n\n"

            if is_thinking and chunk:
                yield f"data: {json.dumps({'choices': [{'delta': {'reasoning_content': chunk}}]})}\n\n"
                continue

            if end_thinking and not chunk:
                continue

            for r in parser.feed(chunk):
                if "text" in r:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"
        mark_active(token_id)
    except (asyncio.CancelledError, GeneratorExit):
        aborted = True
        failed = True
        raise
    except Exception as e:
        failed = True
        m = re.match(r"HTTP (\d{3}):", str(e))
        code = int(m.group(1)) if m else None
        if code in (401, 403, 429):
            mark_limited(token_id)
        logger.exception("stream_response failed")
        try:
            yield f"data: {json.dumps({'error': {'message': str(e)[:300]}})}\n\n"
        except Exception:
            pass
    finally:
        parsed_tools, clean_text = parse_tools(full_text)
        clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
        clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

        if not failed:
            next_messages = messages.copy()
            ast_msg = {"role": "assistant"}
            if parsed_tools:
                ast_msg["tool_calls"] = parsed_tools
            else:
                ast_msg["content"] = clean_text
            next_messages.append(ast_msg)
            next_sig = generate_signature_sync(next_messages, model, scope)
            save_session(sig, token_id, session_id, next_parent(parent_message_id))
            save_session(next_sig, token_id, session_id, next_parent(parent_message_id))

        if not aborted and not failed:
            try:
                if not parsed_tools:
                    for r in parser.flush():
                        if "text" in r:
                            yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"

                if parsed_tools:
                    for i, tc in enumerate(parsed_tools):
                        delta_tc = {"index": i, "id": tc["id"], "type": "function",
                                    "function": {"name": tc["function"]["name"], "arguments": tc["function"]["arguments"]}}
                        yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': [delta_tc]}}]})}\n\n"
                    yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]})}\n\n"
                else:
                    yield f"data: {json.dumps({'choices': [{'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                pass


async def stream_anthropic_response(gen, model, messages, token_id, session_id, sig, tools, req_model=None, parent_message_id=0, scope=""):
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    in_tokens = count_tok(_messages_text(messages))
    model_name = req_model if req_model else model
    start_evt = f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model_name, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': in_tokens, 'output_tokens': 1}}})}\n\n"
    yield start_evt

    parser = StreamToolParser()
    full_text = ""
    text_block_started = False
    block_index = 0
    aborted = False
    failed = False

    try:
        is_thinking = False
        async for chunk in _hold_think_tags(gen):
            if not chunk:
                continue
            full_text += chunk

            if "<think>" in chunk:
                is_thinking = True
                chunk = chunk.replace("<think>", "").lstrip("\n")
                start_block = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'thinking'}})}\n\n"
                yield start_block

            end_thinking = False
            if "</think>" in chunk:
                is_thinking = False
                end_thinking = True
                parts = chunk.split("</think>")
                think_part = parts[0]
                chunk = parts[1].lstrip("\n") if len(parts) > 1 else ""
                if think_part:
                    delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'thinking_delta', 'thinking': think_part}})}\n\n"
                    yield delta_evt

            if is_thinking and chunk:
                delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'thinking_delta', 'thinking': chunk}})}\n\n"
                yield delta_evt
                continue

            if end_thinking:
                stop_evt = f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
                yield stop_evt
                block_index += 1
                if not chunk:
                    continue

            for r in parser.feed(chunk):
                if "text" in r:
                    if not text_block_started:
                        start_block = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        yield start_block
                        text_block_started = True
                    delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': r['text']}})}\n\n"
                    yield delta_evt
        mark_active(token_id)
    except (asyncio.CancelledError, GeneratorExit):
        aborted = True
        failed = True
        raise
    except Exception as e:
        failed = True
        m = re.match(r"HTTP (\d{3}):", str(e))
        code = int(m.group(1)) if m else None
        if code in (401, 403, 429):
            mark_limited(token_id)
        logger.exception("stream_anthropic_response failed")
        try:
            yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)[:300]}})}\n\n"
        except Exception:
            pass
    finally:
        parsed_tools, clean_text = parse_tools(full_text)
        out_tokens = count_tok(full_text)

        clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
        clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

        if not failed:
            next_messages = messages.copy()
            ast_msg = {"role": "assistant"}
            if parsed_tools:
                ast_msg["tool_calls"] = parsed_tools
            else:
                ast_msg["content"] = clean_text
            next_messages.append(ast_msg)
            next_sig = generate_signature_sync(next_messages, model, scope)
            save_session(sig, token_id, session_id, next_parent(parent_message_id))
            save_session(next_sig, token_id, session_id, next_parent(parent_message_id))

        def _tb(text):
            return (f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index_local[0], 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                    f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index_local[0], 'delta': {'type': 'text_delta', 'text': text}})}\n\n"
                    f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index_local[0]})}\n\n")

        block_index_local = [block_index]
        tail_events = ""
        if is_thinking:
            tail_events += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index_local[0]})}\n\n"
            block_index_local[0] += 1

        flushed_text = ""
        if not parsed_tools:
            for r in parser.flush():
                if "text" in r:
                    flushed_text += r["text"]

        if not text_block_started and not parsed_tools and (clean_text or flushed_text):
            tail_events += _tb(clean_text or flushed_text)
        elif text_block_started and not parsed_tools:
            tail_events += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index_local[0]})}\n\n"

        if parsed_tools:
            for tc in parsed_tools:
                tool_input = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
                json_str = json.dumps(tool_input)
                tail_events += f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index_local[0], 'content_block': {'type': 'tool_use', 'id': tc['id'], 'name': tc['function']['name'], 'input': {}}})}\n\n"
                tail_events += f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index_local[0], 'delta': {'type': 'input_json_delta', 'partial_json': json_str}})}\n\n"
                tail_events += f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index_local[0]})}\n\n"
                block_index_local[0] += 1
            tail_events += f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': out_tokens}})}\n\n"
        else:
            tail_events += f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': out_tokens}})}\n\n"
        tail_events += f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"

        if not aborted and not failed:
            try:
                for evt in tail_events.split("\n\n"):
                    if evt.strip():
                        yield evt + "\n\n"
            except asyncio.CancelledError:
                pass


def format_response(text, model, messages, tools=None):
    from functions import DEEPSEEK_TARIFFS
    parsed_tools, clean_text = parse_tools(text)

    reasoning = None
    match = re.search(r"<think>\s*(.*?)\s*</think>\s*", text, flags=re.DOTALL)
    if match:
        reasoning = match.group(1).strip()
    clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

    in_tokens = count_tok(_messages_text(messages))
    out_tokens = count_tok(text)
    tariff_key = "deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash"
    tariff = DEEPSEEK_TARIFFS[tariff_key]
    cost = (in_tokens / 1_000_000 * tariff["cache_miss_input"]) + (out_tokens / 1_000_000 * tariff["output_generation"])

    msg_dict = {
        "role": "assistant",
        "content": clean_text if not parsed_tools else None,
        "tool_calls": parsed_tools if parsed_tools else None,
    }
    if reasoning:
        msg_dict["reasoning_content"] = reasoning

    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": msg_dict,
            "finish_reason": "tool_calls" if parsed_tools else "stop",
        }],
        "usage": {
            "prompt_tokens": in_tokens,
            "completion_tokens": out_tokens,
            "total_tokens": in_tokens + out_tokens,
            "cost": round(cost, 6),
        },
    }


format_openai_response = format_response


def format_anthropic_response(result, model):
    choice = result["choices"][0]
    msg = choice["message"]
    ant_content = []

    if msg.get("reasoning_content"):
        ant_content.append({"type": "thinking", "thinking": msg["reasoning_content"]})

    if msg.get("content"):
        ant_content.append({"type": "text", "text": msg["content"]})

    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            args = tc["function"]["arguments"]
            tool_input = json.loads(args) if isinstance(args, str) else args
            ant_content.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["function"]["name"],
                "input": tool_input,
            })
    usage = result.get("usage", {})
    msg_id = result["id"]
    if not msg_id.startswith("msg_"):
        msg_id = f"msg_{msg_id.replace('chatcmpl-', '')}"
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": ant_content,
        "model": model,
        "stop_reason": "tool_use" if msg.get("tool_calls") else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }



@app.post("/v1/files")
@app.post("/v1/files/upload")
async def files_upload(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    tok_id = pick_token()
    if not tok_id:
        return JSONResponse({"error": "No tokens available"}, status_code=503)
    tok = get_token(tok_id)
    form = await request.form()
    file_obj = form.get("file")
    if not file_obj:
        return JSONResponse({"error": "No file provided"}, status_code=400)
    file_bytes = await file_obj.read(25 * 1024 * 1024 + 1)
    if len(file_bytes) > 25 * 1024 * 1024:
        return JSONResponse({"error": "File too large"}, status_code=413)
    filename = getattr(file_obj, "filename", "file.bin")
    content_type = getattr(file_obj, "content_type", "application/octet-stream")
    file_info = None
    async for status, data in upload_file(file_bytes, filename, content_type, tok["token"]):
        if status == "success":
            file_info = data
            break
    if not file_info:
        return JSONResponse({"error": "Upload failed"}, status_code=500)

    if request.url.path.startswith("/v1/files/upload"):
        return {
            "id": file_info["file_id"],
            "type": "file",
            "filename": filename,
            "size": file_info["size"],
            "created_at": file_info["anthropic_timestamp"],
        }
    return {
        "id": file_info["file_id"],
        "object": "file",
        "bytes": file_info["size"],
        "created_at": file_info["openai_timestamp"],
        "filename": filename,
        "purpose": "answers",
    }


@app.get("/v1/files/{file_id}/content")
@app.get("/v1/files/{file_id}")
async def files_content(file_id: str, request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    tok_id = pick_token()
    if not tok_id:
        return JSONResponse({"error": "No tokens available"}, status_code=503)
    tok = get_token(tok_id)
    gen = get_file_content(tok["token"], file_id)
    try:
        mime = await gen.__anext__()
    except StopAsyncIteration:
        return JSONResponse({"error": "File not found"}, status_code=404)
    except Exception:
        return JSONResponse({"error": "File fetch failed"}, status_code=502)
    async def stream_chunks():
        async for chunk in gen:
            yield chunk
    return StreamingResponse(stream_chunks(), media_type=mime or "application/octet-stream")


def is_thinking_enabled(body, request=None):
    effort = body.get("effort")
    if effort is not None:
        e_str = str(effort).strip().lower()
        if e_str in ["medium", "high", "max", "ultra", "extreme", "enabled", "adaptive", "on"]:
            return True
        if e_str in ["low", "none", "off", "disable", "disabled", "false"]:
            return False

    out_cfg = body.get("output_config")
    if isinstance(out_cfg, dict):
        out_effort = out_cfg.get("effort") or out_cfg.get("reasoning_effort")
        if out_effort is not None:
            e_str = str(out_effort).strip().lower()
            if e_str in ["medium", "high", "max", "ultra", "extreme", "enabled", "adaptive", "on"]:
                return True
            if e_str in ["low", "none", "off", "disable", "disabled", "false"]:
                return False

    thinking_val = body.get("thinking")
    if isinstance(thinking_val, dict):
        t_type = str(thinking_val.get("type", "")).strip().lower()
        if t_type in ["enabled", "adaptive", "true"]:
            return True
        if t_type == "disabled":
            return False
        budget = thinking_val.get("budget_tokens", 0)
        if isinstance(budget, (int, float)) and budget > 0:
            return True
        t_effort = thinking_val.get("effort") or thinking_val.get("reasoning_effort") or thinking_val.get("level")
        if t_effort is not None:
            e_str = str(t_effort).strip().lower()
            if e_str in ["medium", "high", "max", "ultra", "extreme", "enabled", "adaptive", "on"]:
                return True
            if e_str in ["low", "none", "off", "disable", "disabled", "false"]:
                return False
    elif isinstance(thinking_val, str):
        t_str = thinking_val.strip().lower()
        if t_str in ["medium", "high", "max", "ultra", "extreme", "true", "enabled", "adaptive", "on"]:
            return True
        if t_str in ["low", "none", "off", "disable", "disabled", "false"]:
            return False
    elif isinstance(thinking_val, bool):
        return thinking_val

    reasoning_effort = body.get("reasoning_effort")
    if reasoning_effort is not None:
        effort_str = str(reasoning_effort).strip().lower()
        if effort_str in ["medium", "high", "max", "ultra", "extreme"]:
            return True
        if effort_str in ["low", "none", "off", "disable", "disabled"]:
            return False

    if request:
        req_effort = request.headers.get("anthropic-thinking") or request.headers.get("x-anthropic-thinking") or request.headers.get("effort") or request.headers.get("x-effort")
        if req_effort:
            e_str = str(req_effort).strip().lower()
            if e_str in ["medium", "high", "max", "ultra", "extreme", "enabled", "adaptive", "on"]:
                return True
    return False


def resolve_model(model_raw):
    if not model_raw or not isinstance(model_raw, str):
        return "expert"
    m = model_raw.lower()
    if "instant" in m or "haiku" in m or "flash" in m:
        return "instant"
    elif "vision" in m:
        return "vision"
    return "expert"


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    messages = body.get("messages", [])
    model = resolve_model(body.get("model", "expert"))
    thinking = is_thinking_enabled(body, request)
    search = body.get("search", False)
    stream = body.get("stream", False)
    tools = body.get("tools", None)
    return await handle_chat(messages, model, thinking, search, stream, tools, scope=get_api_key(request))


@app.post("/v1/responses")
async def openai_responses(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    model = resolve_model(body.get("model", "expert"))
    inputs = body.get("input", [])
    if isinstance(inputs, str):
        inputs = [inputs]
    elif isinstance(inputs, dict):
        inputs = [inputs]

    messages = []
    for item in inputs:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue

        role = item.get("role", "user")
        content = item.get("content", [])
        msg_content = []
        if isinstance(content, str):
            msg_content = content
        else:
            for c in content:
                if c.get("type") == "input_text":
                    msg_content.append({"type": "text", "text": c.get("text")})
                elif c.get("type") == "input_file":
                    msg_content.append({"type": "file", "file_id": c.get("file_id")})
                else:
                    msg_content.append(c)
        messages.append({"role": role, "content": msg_content})

    thinking = is_thinking_enabled(body, request)
    search = body.get("search", False)
    stream = body.get("stream", False)
    tools = body.get("tools", None)

    result = await handle_chat(messages, model, thinking, search, stream, tools, scope=get_api_key(request))

    if stream:
        return result

    if isinstance(result, dict) and "choices" in result:
        message = result["choices"][0]["message"]
        out_content = []
        if message.get("content"):
            out_content.append({"type": "text", "text": message["content"]})
        if message.get("tool_calls"):
            out_content.extend([{"type": "tool_call", "id": tc["id"], "name": tc["function"]["name"], "arguments": tc["function"]["arguments"]} for tc in message["tool_calls"]])

        msg_output = {
            "type": "message",
            "role": "assistant",
            "content": out_content
        }
        if message.get("reasoning_content"):
            msg_output["reasoning_content"] = message["reasoning_content"]

        return {
            "id": result["id"],
            "object": "response",
            "model": result["model"],
            "output": [msg_output],
            "usage": result.get("usage", {})
        }
    return result


@app.post("/v1/messages")
@app.post("/messages")
async def anthropic_messages(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    system = body.get("system", "")

    messages = body.get("messages", [])
    model = resolve_model(body.get("model", "expert"))

    thinking = is_thinking_enabled(body, request)
    stream = body.get("stream", False)
    tools = body.get("tools", [])

    openai_msgs = []
    if system:
        if isinstance(system, list):
            system_str = " ".join(c.get("text", "") for c in system if isinstance(c, dict) and c.get("type") == "text")
        else:
            system_str = str(system)
        if system_str:
            openai_msgs.append({"role": "system", "content": system_str})

    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            parts = []
            image_parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "image":
                        image_parts.append(c)
                    elif c.get("type") == "tool_use":
                        parts.append(f"{json.dumps({'name': c.get('name'), 'arguments': c.get('input', {})})}")
                    elif c.get("type") == "tool_result":
                        res_content = c.get("content", "")
                        if isinstance(res_content, list):
                            for item in res_content:
                                if isinstance(item, dict) and item.get("type") == "image":
                                    image_parts.append(item)
                            res_content = " ".join(item.get("text", "") for item in res_content if isinstance(item, dict) and item.get("type") == "text")
                        parts.append(f"[Tool Result for {c.get('tool_use_id', 'tool')}]: {res_content}")
            if image_parts:
                content = [{"type": "text", "text": s} for s in parts if s] + image_parts
            else:
                content = "\n".join(parts)
        if m.get("role") == "system":
            if content:
                openai_msgs.append({"role": "system", "content": content})
            continue
        if m["role"] == "assistant" and (not content or content.strip() == "(no content)"):
            continue
        openai_msgs.append({"role": m["role"], "content": content})

    openai_tools = []
    for t in tools:
        if t.get("type") == "function":
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", "NO DESCRIPTION"),
                    "parameters": t.get("input_schema", {}),
                },
            })
        elif "name" in t:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            })

    output_config = body.get("output_config")
    if isinstance(output_config, dict) and output_config.get("format", {}).get("type") == "json_schema":
        json_schema = output_config["format"].get("schema")
        if json_schema:
            openai_msgs.insert(0, {"role": "system", "content": f"You MUST return valid JSON adhering strictly to this JSON Schema:\n{json.dumps(json_schema)}"})

    req_model = body.get("model")
    if stream:
        return await handle_chat(openai_msgs, model, thinking, False, True, openai_tools or None, is_anthropic=True, req_model=req_model, scope=get_api_key(request))

    result = await handle_chat(openai_msgs, model, thinking, False, False, openai_tools or None, is_anthropic=True, req_model=req_model, scope=get_api_key(request))
    if not isinstance(result, dict) or "choices" not in result:
        return result
    return format_anthropic_response(result, req_model)


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)

    base_models = [
        {
            "id": "instant",
            "object": "model",
            "type": "model",
            "name": "instant",
            "display_name": "Instant",
            "created": 1785456000,
            "created_at": "2026-07-31T00:00:00Z",
            "owned_by": "deeperseeker",
            "capabilities": {
                "batch": {"supported": True},
                "structured_outputs": {"supported": True},
                "thinking": {
                    "supported": True,
                    "types": {
                        "enabled": {"supported": True},
                        "adaptive": {"supported": True}
                    }
                },
                "effort": {
                    "supported": True,
                    "low": {"supported": True},
                    "medium": {"supported": True}
                },
                "context_management": {
                    "clear_thinking_20251015": {"supported": True},
                    "compact_20260112": {"supported": True},
                    "supported": True
                }
            }
        },
        {
            "id": "expert",
            "object": "model",
            "type": "model",
            "name": "expert",
            "display_name": "Expert",
            "created": 1788134400,
            "created_at": "2026-08-31T00:00:00Z",
            "owned_by": "deeperseeker",
            "capabilities": {
                "batch": {"supported": True},
                "code_execution": {"supported": True},
                "structured_outputs": {"supported": True},
                "thinking": {
                    "supported": True,
                    "types": {
                        "enabled": {"supported": True},
                        "adaptive": {"supported": True}
                    }
                },
                "effort": {
                    "supported": True,
                    "low": {"supported": True},
                    "medium": {"supported": True}
                },
                "context_management": {
                    "clear_thinking_20251015": {"supported": True},
                    "compact_20260112": {"supported": True},
                    "supported": True
                }
            }
        },
        {
            "id": "vision",
            "object": "model",
            "type": "model",
            "name": "vision",
            "display_name": "Vision",
            "created": 1785456000,
            "created_at": "2026-07-31T00:00:00Z",
            "owned_by": "deeperseeker",
            "capabilities": {
                "batch": {"supported": True},
                "image_input": {"supported": True},
                "pdf_input": {"supported": True},
                "structured_outputs": {"supported": True},
                "thinking": {
                    "supported": True,
                    "types": {
                        "enabled": {"supported": True},
                        "adaptive": {"supported": True}
                    }
                },
                "effort": {
                    "supported": True,
                    "low": {"supported": True},
                    "medium": {"supported": True}
                },
                "context_management": {
                    "clear_thinking_20251015": {"supported": True},
                    "compact_20260112": {"supported": True},
                    "supported": True
                }
            }
        }
    ]

    claude_aliases = []
    for m in base_models:
        alias = dict(m)
        alias["id"] = f"anthropic/claude-{m['id']}"
        alias["name"] = f"anthropic/claude-{m['name']}"
        alias["display_name"] = f"Claude {m['display_name']}"
        claude_aliases.append(alias)

    all_models = base_models + claude_aliases

    return {
        "object": "list",
        "data": all_models,
        "has_more": False,
        "first_id": all_models[0]["id"],
        "last_id": all_models[-1]["id"]
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if time.time() < _login_fails["locked_until"]:
        return templates.TemplateResponse(request, "login.html", {"error": "Too many attempts. Try again later."})
    if secrets.compare_digest(username.encode("utf-8"), ADMIN_USER.encode("utf-8")) and secrets.compare_digest(password.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        _login_fails["count"] = 0
        sid = str(uuid.uuid4())
        SESSIONS[sid] = time.time()
        resp = HTMLResponse("<meta http-equiv='refresh' content='0;url=/dashboard'>")
        resp.set_cookie("session_id", sid, httponly=True, samesite="lax")
        return resp
    _login_fails["count"] += 1
    if _login_fails["count"] >= 5:
        _login_fails["locked_until"] = time.time() + 300
        _login_fails["count"] = 0
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("session_id")
    SESSIONS.pop(sid, None)
    resp = HTMLResponse("<meta http-equiv='refresh' content='0;url=/login'>")
    resp.delete_cookie("session_id")
    return resp


@app.get("/dashboard")
async def dashboard(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        return HTMLResponse("<meta http-equiv='refresh' content='0;url=/login'>")
    tokens = get_tokens()
    return templates.TemplateResponse(request, "dashboard.html", {"tokens": tokens})


@app.post("/tokens/add")
async def tokens_add(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        return HTMLResponse("<meta http-equiv='refresh' content='0;url=/login'>")
    form = await request.form()
    auth_token = form.get("auth_token", "").strip().strip("'\"")
    alias = form.get("alias", "").strip() or None
    if auth_token:
        add_token(auth_token, alias)
    return HTMLResponse("<meta http-equiv='refresh' content='0;url=/dashboard'>")


@app.post("/tokens/{token_id}/delete")
async def tokens_delete(token_id: int, request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        return HTMLResponse("<meta http-equiv='refresh' content='0;url=/login'>")
    delete_token(token_id)
    return HTMLResponse("<meta http-equiv='refresh' content='0;url=/dashboard'>")


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return await dashboard(request)


@app.get("/health")
async def health(request: Request):
    active = sum(1 for t in get_tokens() if t["status"] == "ACTIVE")
    cookies_valid = False
    try:
        with open("aws_cookies_deepseek.json") as f:
            c = json.load(f)
        exp = c.get("expiry")
        cookies_valid = bool(exp and exp > time.time())
    except Exception:
        cookies_valid = False
    ok = active > 0 and cookies_valid
    data = {"status": "ok" if ok else "degraded"}
    if check_key(request):
        data["active_tokens"] = active
        data["cookies_valid"] = cookies_valid
    return JSONResponse(data, status_code=200 if ok else 503)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "4000")))
