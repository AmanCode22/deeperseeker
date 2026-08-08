import asyncio
import json
import os
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
    save_auth_token,
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
    print("DeeperSeeker started on http://0.0.0.0:4000")
    print("Dashboard: http://localhost:4000/")
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


async def handle_chat(messages, model, thinking=False, search=False, stream=False, tools=None):
    auth_token = get_auth_token()
    if not auth_token:
        return JSONResponse({"error": "No auth token. Add via dashboard."}, status_code=401)

    sig = await generate_signature(messages)
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
                        return StreamingResponse(stream_response(gen, model, messages, new_token_id, new_session_id, sig, tools), media_type="text/event-stream")
                    else:
                        resp_text = await collect_response(gen)
                        save_session(sig, new_token_id, new_session_id, 2)
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
            return StreamingResponse(stream_response(gen, model, messages, token_id, session_id, sig, tools), media_type="text/event-stream")
        else:
            resp_text = await collect_response(gen)
            save_session(sig, token_id, session_id, parent_message_id + 2)
            return format_response(resp_text, model, messages, tools)
    except Exception as e:
        if "429" in str(e) or "rate" in str(e).lower():
            mark_limited(token_id)
            return await handle_chat(messages, model, thinking, search, stream, tools)
        raise


async def extract_user_msg_text(messages):
    for msg in reversed(messages):
        if msg["role"] == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for c in content:
                    if c.get("type") == "text":
                        return c["text"]
    return ""


async def collect_response(gen):
    text = ""
    async for chunk in gen:
        text += chunk
    return text


async def stream_response(gen, model, messages, token_id, session_id, sig, tools):
    parser = StreamToolParser()
    full_text = ""
    try:
        async for chunk in gen:
            full_text += chunk
            results = parser.feed(chunk)
            for r in results:
                if "text" in r:
                    yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"
    except Exception:
        pass
    for r in parser.flush():
        if "text" in r:
            yield f"data: {json.dumps({'choices': [{'delta': {'content': r['text']}}]})}\n\n"

    parsed_tools, clean_text = parse_tools(full_text)
    if parsed_tools:
        yield f"data: {json.dumps({'choices': [{'delta': {'tool_calls': parsed_tools}, 'finish_reason': 'tool_calls'}]})}\n\n"
    else:
        yield f"data: {json.dumps({'choices': [{'finish_reason': 'stop'}]})}\n\n"
    yield "data: [DONE]\n\n"

    in_tokens = count_tok(json.dumps(messages))
    out_tokens = count_tok(full_text)
    save_session(sig, token_id, session_id, 0)


def format_response(text, model, messages, tools=None):
    from functions import DEEPSEEK_TARIFFS
    parsed_tools, clean_text = parse_tools(text)
    in_tokens = count_tok(json.dumps(messages))
    out_tokens = count_tok(text)
    tariff_key = "deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash"
    tariff = DEEPSEEK_TARIFFS[tariff_key]
    cost = (in_tokens / 1_000_000 * tariff["cache_miss_input"]) + (out_tokens / 1_000_000 * tariff["output_generation"])
    return {
        "id": f"chatcmpl-{uuid.uuid4()}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": clean_text if not parsed_tools else None,
                "tool_calls": parsed_tools if parsed_tools else None,
            },
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
    if msg.get("content"):
        ant_content.append({"type": "text", "text": msg["content"]})
    ant_tools = []
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            ant_tools.append({
                "type": "tool_use",
                "id": tc["id"],
                "name": tc["function"]["name"],
                "input": json.loads(tc["function"]["arguments"]),
            })
    return {
        "id": result["id"],
        "type": "message",
        "role": "assistant",
        "content": ant_content + ant_tools,
        "model": model,
        "stop_reason": "tool_use" if ant_tools else "end_turn",
        "usage": result["usage"],
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


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    messages = body.get("messages", [])
    model = body.get("model", "instant")
    thinking = body.get("thinking", False)
    search = body.get("search", False)
    stream = body.get("stream", False)
    tools = body.get("tools", None)
    return await handle_chat(messages, model, thinking, search, stream, tools)


@app.post("/v1/messages")
async def anthropic_messages(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    body = await request.json()
    system = body.get("system", "")
    messages = body.get("messages", [])
    model = body.get("model", "instant")
    thinking = body.get("thinking", False)
    stream = body.get("stream", False)
    tools = body.get("tools", [])

    openai_msgs = []
    if system:
        openai_msgs.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, list):
            text_parts = [c.get("text", "") for c in content if c.get("type") == "text"]
            content = " ".join(text_parts)
        openai_msgs.append({"role": m["role"], "content": content})

    openai_tools = []
    for t in tools:
        if t.get("type") == "function":
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                },
            })

    result = await handle_chat(openai_msgs, model, thinking, False, stream, openai_tools or None)
    if isinstance(result, StreamingResponse):
        return result

    return format_anthropic_response(result, model)


@app.get("/v1/models")
@app.get("/models")
async def list_models(request: Request):
    if not check_key(request):
        return JSONResponse({"error": "Invalid API key"}, status_code=401)
    models = ["instant", "vision", "expert"]
    return {
        "object": "list",
        "data": [
            {
                "id": m,
                "object": "model",
                "created": 1700000000,
                "owned_by": "deeperseeker",
            }
            for m in models
        ],
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
        save_auth_token(auth_token)
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
