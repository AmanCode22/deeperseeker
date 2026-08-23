import asyncio
import json
import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import deepseek_tokenizer
import uvicorn
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

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
    parse_tools,
    pick_token,
    save_session,
    save_session_map,
    send_message,
    StreamToolParser,
    summarize_messages,
    upload_file,
    get_file_content,
)
from plugin_helper import build_prompt, extract_and_upload_files, generate_signature


def count_tok(text):
    return len(deepseek_tokenizer.ds_token.encode(text))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="DeeperSeeker", lifespan=lifespan)
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")


SESSIONS = set()


def get_current_admin(request: Request):
    sid = request.cookies.get("session_id")
    if not sid or sid not in SESSIONS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return "admin"


def check_key(request: Request):
    auth = request.headers.get("authorization", "")
    api_key_header = request.headers.get("x-api-key", "")
    if auth.startswith("Bearer "):
        key = auth[7:]
    elif auth:
        key = auth
    else:
        key = api_key_header
    return key == API_KEY


async def handle_chat(messages, model, thinking=False, search=False, stream=False, tools=None, is_anthropic=False, req_model=None):
    auth_token = get_auth_token()
    if not auth_token:
        return JSONResponse({"error": "No auth token. Add via dashboard."}, status_code=401)

    sig = await generate_signature(messages, model)
    sess = find_session(sig)

    if sess:
        token_id = sess["token_id"]
        session_id = sess["session_id"]
        parent_message_id = sess["parent_message_id"]
        tok = get_token(token_id)
        if tok and tok["status"] == "RATE_LIMITED":
            summary = summarize_messages(messages)
            new_token_id = pick_token()
            if new_token_id and new_token_id != token_id:
                new_tok = get_token(new_token_id)
                if new_tok:
                    new_session_id = await create_new_chat(new_tok["token"])
                    save_session_map(session_id, new_session_id, new_token_id)
                    prompt = await build_prompt(messages, tools or [], model, is_first_message=True)
                    gen = send_message(new_session_id, new_tok["token"], prompt, 0, thinking, search, None if model == "instant" else model)
                    if stream:
                        if is_anthropic:
                            return StreamingResponse(stream_anthropic_response(gen, model, messages, new_token_id, new_session_id, sig, tools, req_model, parent_message_id), media_type="text/event-stream")
                        return StreamingResponse(stream_response(gen, model, messages, new_token_id, new_session_id, sig, tools, parent_message_id), media_type="text/event-stream")
                    else:
                        resp_text = await collect_response(gen)

                        parsed_tools, clean_text = parse_tools(resp_text)
                        clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
                        next_messages = messages.copy()
                        ast_msg = {"role": "assistant"}
                        if parsed_tools:
                            ast_msg["tool_calls"] = parsed_tools
                        else:
                            ast_msg["content"] = clean_text
                        next_messages.append(ast_msg)
                        next_sig = await generate_signature(next_messages, model)

                        save_session(sig, new_token_id, new_session_id, 2)
                        save_session(next_sig, new_token_id, new_session_id, 2)
                        return format_response(resp_text, model, messages, tools)
    else:
        token_id = pick_token()
        if not token_id:
            return JSONResponse({"error": "No tokens available"}, status_code=503)
        tok = get_token(token_id)
        if not tok:
            return JSONResponse({"error": "Token not found"}, status_code=503)
        session_id = await create_new_chat(tok["token"])
        parent_message_id = 0

    tok = get_token(token_id)
    if not tok:
        return JSONResponse({"error": "Token expired"}, status_code=503)

    file_ids = await extract_and_upload_files(messages, tok["token"])
    is_first = parent_message_id == 0
    prompt = await build_prompt(messages, tools or [], model, is_first)

    try:
        gen = send_message(session_id, tok["token"], prompt, parent_message_id, thinking, search, None if model == "instant" else model, file_ids)
        if stream:
            if is_anthropic:
                return StreamingResponse(stream_anthropic_response(gen, model, messages, token_id, session_id, sig, tools, req_model, parent_message_id), media_type="text/event-stream")
            return StreamingResponse(stream_response(gen, model, messages, token_id, session_id, sig, tools, parent_message_id), media_type="text/event-stream")
        else:
            resp_text = await collect_response(gen)

            parsed_tools, clean_text = parse_tools(resp_text)
            clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
            next_messages = messages.copy()
            ast_msg = {"role": "assistant"}
            if parsed_tools:
                ast_msg["tool_calls"] = parsed_tools
            else:
                ast_msg["content"] = clean_text
            next_messages.append(ast_msg)
            next_sig = await generate_signature(next_messages, model)

            save_session(sig, token_id, session_id, parent_message_id + 2)
            save_session(next_sig, token_id, session_id, parent_message_id + 2)
            return format_response(resp_text, model, messages, tools)
    except Exception as e:
        if "429" in str(e) or "rate" in str(e).lower():
            mark_limited(token_id)
            return await handle_chat(messages, model, thinking, search, stream, tools, is_anthropic, req_model)
        raise


async def extract_user_msg_text(messages):
    for msg in reversed(messages):
        if msg["role"] == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
                if parts:
                    return "\n".join(parts)
    return ""


async def collect_response(gen):
    text = ""
    async for chunk in gen:
        text += chunk
    return text


async def stream_response(gen, model, messages, token_id, session_id, sig, tools, parent_message_id=0):
    parser = StreamToolParser()
    full_text = ""
    try:
        is_thinking = False
        async for chunk in gen:
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

            results = parser.feed(chunk)
            for r in results:
                if "text" in r and not parser.has_tool:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"
    except Exception:
        pass

    parsed_tools, clean_text = parse_tools(full_text)
    clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

    if not parsed_tools:
        for r in parser.flush():
            if "text" in r:
                yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"

    if parsed_tools:
        yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': parsed_tools}, 'finish_reason': 'tool_calls'}]})}\n\n"
    else:
        yield f"data: {json.dumps({'choices': [{'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"

    next_messages = messages.copy()
    ast_msg = {"role": "assistant"}
    if parsed_tools:
        ast_msg["tool_calls"] = parsed_tools
    else:
        ast_msg["content"] = clean_text
    next_messages.append(ast_msg)
    next_sig = await generate_signature(next_messages, model)
    save_session(sig, token_id, session_id, parent_message_id + 2)
    save_session(next_sig, token_id, session_id, parent_message_id + 2)


async def stream_anthropic_response(gen, model, messages, token_id, session_id, sig, tools, req_model=None, parent_message_id=0):
    msg_id = f"msg_{uuid.uuid4().hex[:24]}"
    in_tokens = count_tok(json.dumps(messages))
    model_name = req_model if req_model else model
    start_evt = f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'id': msg_id, 'type': 'message', 'role': 'assistant', 'content': [], 'model': model_name, 'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': in_tokens, 'output_tokens': 1}}})}\n\n"
    yield start_evt

    parser = StreamToolParser()
    full_text = ""
    text_block_started = False
    block_index = 0

    try:
        is_thinking = False
        async for chunk in gen:
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

            results = parser.feed(chunk)
            for r in results:
                if "text" in r and not parser.has_tool:
                    if not text_block_started:
                        start_block = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                        yield start_block
                        text_block_started = True
                    delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': r['text']}})}\n\n"
                    yield delta_evt
    except Exception:
        pass

    parsed_tools, clean_text = parse_tools(full_text)
    out_tokens = count_tok(full_text)

    if not parsed_tools:
        for r in parser.flush():
            if "text" in r:
                if not text_block_started:
                    start_block = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
                    yield start_block
                    text_block_started = True
                delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': r['text']}})}\n\n"
                yield delta_evt

    clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

    if not text_block_started and not parsed_tools and clean_text:
        start_evt = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'text', 'text': ''}})}\n\n"
        yield start_evt
        delta_evt = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'text_delta', 'text': clean_text}})}\n\n"
        yield delta_evt
        stop_evt = f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
        yield stop_evt
        block_index += 1
    elif text_block_started:
        stop_evt = f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
        yield stop_evt
        block_index += 1

    if parsed_tools:
        for tc in parsed_tools:
            tool_input = json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"]
            json_str = json.dumps(tool_input)
            start_tc = f"event: content_block_start\ndata: {json.dumps({'type': 'content_block_start', 'index': block_index, 'content_block': {'type': 'tool_use', 'id': tc['id'], 'name': tc['function']['name'], 'input': {}}})}\n\n"
            yield start_tc
            delta_tc = f"event: content_block_delta\ndata: {json.dumps({'type': 'content_block_delta', 'index': block_index, 'delta': {'type': 'input_json_delta', 'partial_json': json_str}})}\n\n"
            yield delta_tc
            stop_tc = f"event: content_block_stop\ndata: {json.dumps({'type': 'content_block_stop', 'index': block_index})}\n\n"
            yield stop_tc
            block_index += 1
        msg_delta = f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'tool_use', 'stop_sequence': None}, 'usage': {'output_tokens': out_tokens}})}\n\n"
        yield msg_delta
    else:
        msg_delta = f"event: message_delta\ndata: {json.dumps({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': out_tokens}})}\n\n"
        yield msg_delta

    stop_evt = f"event: message_stop\ndata: {json.dumps({'type': 'message_stop'})}\n\n"
    yield stop_evt

    next_messages = messages.copy()
    clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
    ast_msg = {"role": "assistant"}
    if parsed_tools:
        ast_msg["tool_calls"] = parsed_tools
    else:
        ast_msg["content"] = clean_text
    next_messages.append(ast_msg)
    next_sig = await generate_signature(next_messages, model)

    save_session(sig, token_id, session_id, parent_message_id + 2)
    save_session(next_sig, token_id, session_id, parent_message_id + 2)


def format_response(text, model, messages, tools=None):
    from functions import DEEPSEEK_TARIFFS
    parsed_tools, clean_text = parse_tools(text)
    
    reasoning = None
    match = re.search(r"<think>\s*(.*?)\s*</think>\s*", text, flags=re.DOTALL)
    if match:
        reasoning = match.group(1).strip()
    clean_text = re.sub(r"<think>.*?</think>", "", clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()
        
    in_tokens = count_tok(json.dumps(messages))
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
    file_bytes = await file_obj.read()
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
    mime = await gen.__anext__()
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
            if e_str in ["low", "none", "off", "disable", "disabled", "false"]:
                return False

    return False


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "instant")
    thinking = is_thinking_enabled(body, request)
    search = body.get("search", False)
    stream = body.get("stream", False)
    tools = body.get("tools", None)
    return await handle_chat(messages, model, thinking, search, stream, tools)


@app.post("/v1/responses")
async def openai_responses(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    model = body.get("model", "instant")
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

    result = await handle_chat(messages, model, thinking, search, stream, tools)

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
    model_raw = body.get("model", "").lower()
    if "instant" in model_raw:
        model = "instant"
    elif "vision" in model_raw:
        model = "vision"
    else:
        model = "expert"

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
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_use":
                        parts.append(f"<tool_call>{json.dumps({'name': c.get('name'), 'arguments': c.get('input', {})})}</tool_call>")
                    elif c.get("type") == "tool_result":
                        res_content = c.get("content", "")
                        if isinstance(res_content, list):
                            res_content = " ".join(item.get("text", "") for item in res_content if isinstance(item, dict) and item.get("type") == "text")
                        parts.append(f"[Tool Result for {c.get('tool_use_id', 'tool')}]: {res_content}")
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
        return await handle_chat(openai_msgs, model, thinking, False, True, openai_tools or None, is_anthropic=True, req_model=req_model)

    result = await handle_chat(openai_msgs, model, thinking, False, False, openai_tools or None, is_anthropic=True, req_model=req_model)
    return format_anthropic_response(result, req_model)


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)

    return {
        "object": "list",
        "data": [
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
        ],
        "has_more": False,
        "first_id": "instant",
        "last_id": "vision"
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if username == ADMIN_USER and password == ADMIN_PASSWORD:
        sid = str(uuid.uuid4())
        SESSIONS.add(sid)
        resp = HTMLResponse("<meta http-equiv='refresh' content='0;url=/dashboard'>")
        resp.set_cookie("session_id", sid, httponly=True)
        return resp
    return templates.TemplateResponse(request, "login.html", {"error": "Invalid username or password"})


@app.get("/logout")
async def logout(request: Request):
    sid = request.cookies.get("session_id")
    if sid in SESSIONS:
        SESSIONS.remove(sid)
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
async def health():
    return {"status": "ok"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=4000)
