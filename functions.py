import asyncio
import base64
import json
import mimetypes
import os
import sqlite3
import time
from datetime import datetime, timezone

import aiohttp
import wasmtime
from playwright.async_api import async_playwright

wasm_path = "wasm/deepseek_pow_solver.wasm"

_session = None


async def get_session():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session


def get_headers(auth_token, pow=None):
    headers = {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "authorization": f"Bearer {auth_token}",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "referer": "https://chat.deepseek.com/",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
        "x-app-version": "2.0.0",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-version": "2.0.0",
    }

    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


async def get_cookies():
    if not os.path.exists("aws_cookies_deepseek.json"):
        print("Cookie not found or expired, regenrating, this might take a few moments.....")
        await _generate_cookies()
    else:
        cookies = json.load(open("aws_cookies_deepseek.json"))
        if cookies.get("expiry") is None or cookies["expiry"] <= time.time():
            print("Cookie not found or expired, regenrating, this might take a few moments.....")
            await _generate_cookies()
            cookies = json.load(open("aws_cookies_deepseek.json"))
    return cookies["cookie"]


async def _generate_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, args=["--window-position=-32000,-32000"]
        )
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
        await page.wait_for_selector("body")
        cookies = await context.cookies()
        final_cookies = {}
        expiry = None
        for i in cookies:
            if i.get("name") == "aws-waf-token":
                expiry = i["expires"]
            final_cookies[i["name"]] = i["value"]
        final_cookies.update(
            {
                "ds_cookie_preference": "%257B%2522level%2522%253A%2522all%2522%257D",
            }
        )
        await browser.close()
    with open("aws_cookies_deepseek.json", "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))


async def login_account(email=None, mobile=None, area_code="", password=""):
    cookie = await get_cookies()
    session = await get_session()
    url = "https://chat.deepseek.com/api/v0/users/login"
    headers = get_headers(None)
    json_data = {
        "email": email,
        "mobile": mobile,
        "password": password,
        "area_code": area_code,
        "device_id": "",
        "os": "web",
    }
    async with session.post(
        url, cookies=cookie, headers=headers, json=json_data
    ) as response:
        if response.status != 200:
            return None
        data = await response.json()
    try:
        if data["data"]["biz_data"]["biz_code"] != 0:
            return None
        return data["data"]["biz_data"]["user"]["token"]
    except (KeyError, TypeError):
        return None


def init_db():
    conn = sqlite3.connect("deeperseeker.db")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            mobile TEXT,
            area_code TEXT,
            password TEXT,
            token TEXT,
            status TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            signature TEXT PRIMARY KEY,
            account_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER
        )"""
    )
    conn.commit()
    conn.close()


def add_account(email=None, mobile=None, area_code="", password=""):
    conn = sqlite3.connect("deeperseeker.db")
    cur = conn.execute(
        """INSERT INTO accounts (email, mobile, area_code, password, token, status)
           VALUES (?, ?, ?, ?, NULL, ?)""",
        (email, mobile, area_code, password, "NOT_LOGGED_IN"),
    )
    account_id = cur.lastrowid
    conn.commit()
    conn.close()
    return account_id


async def get_account_token(account_id):
    conn = sqlite3.connect("deeperseeker.db")
    row = conn.execute(
        "SELECT email, mobile, area_code, password, token, status FROM accounts WHERE id = ?",
        (account_id,),
    ).fetchone()
    if row is None:
        conn.close()
        return None
    email, mobile, area_code, password, token, status = row
    if status == "RUNNING" and token:
        conn.close()
        return token
    token = await login_account(
        email=email, mobile=mobile, area_code=area_code, password=password
    )
    if token is None:
        conn.execute(
            "UPDATE accounts SET status = ? WHERE id = ?",
            ("NOT_LOGGED_IN", account_id),
        )
        conn.commit()
        conn.close()
        return None
    conn.execute(
        "UPDATE accounts SET token = ?, status = ? WHERE id = ?",
        (token, "RUNNING", account_id),
    )
    conn.commit()
    conn.close()
    return token


async def get_account_rate_limit(signature):
    conn = sqlite3.connect("deeperseeker.db")
    row = conn.execute(
        "SELECT account_id FROM sessions WHERE signature = ?",
        (signature,),
    ).fetchone()
    if row is not None:
        account_id = row[0]
        status = conn.execute(
            "SELECT status FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if status is not None and status[0] == "RUNNING":
            conn.close()
            return account_id
    account = conn.execute(
        "SELECT id FROM accounts WHERE status = ? ORDER BY id LIMIT 1",
        ("RUNNING",),
    ).fetchone()
    if account is None:
        account = conn.execute(
            "SELECT id FROM accounts ORDER BY id LIMIT 1",
        ).fetchone()
    account_id = account[0] if account is not None else None
    conn.close()
    return account_id


async def send_history_new_account(
    signature, account_id, messages, model, thinking=False, search=False
):
    from plugin_helper import build_prompt, generate_signature

    token = await get_account_token(account_id)
    if token is None:
        return
    session_id = await create_new_chat(token)
    parent_message_id = 0
    tools = []
    prompt = await build_prompt(
        messages, tools, model, is_first_message=True
    )
    sig = await generate_signature(messages)
    conn = sqlite3.connect("deeperseeker.db")
    conn.execute(
        """INSERT OR REPLACE INTO sessions
           (signature, account_id, deepseek_session_id, parent_message_id)
           VALUES (?, ?, ?, ?)""",
        (sig, account_id, session_id, parent_message_id),
    )
    conn.commit()
    conn.close()
    generator = send_message(
        session_id,
        token,
        prompt,
        parent_message_id,
        thinking=thinking,
        search=search,
        model_type=None if model == "instant" else model,
    )
    async for chunk in generator:
        yield chunk


def _find_pow_answer_blocking(challange_data):
    engine = wasmtime.Engine()
    with open(wasm_path, "rb") as f:
        wasm_bytes = f.read()
    module = wasmtime.Module(engine, wasm_bytes)
    store = wasmtime.Store(engine)
    linker = wasmtime.Linker(engine)
    instance = linker.instantiate(store, module)
    memory = instance.exports(store)["memory"]
    alloc_func = instance.exports(store)["alloc"]
    solve_func = instance.exports(store)["solve_pow"]
    ch_ptr, ch_len = write_string_pow(
        challange_data["challenge"], alloc_func, memory, store
    )
    salt_ptr, salt_len = write_string_pow(
        challange_data["salt"], alloc_func, memory, store
    )
    result = solve_func(
        store,
        ch_ptr,
        ch_len,
        salt_ptr,
        salt_len,
        challange_data["expire_at"],
        challange_data["difficulty"],
    )
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


async def create_challange_pow(target_path, auth_token):
    headers = get_headers(auth_token)
    cookie = await get_cookies()
    session = await get_session()
    json_data = {
        "target_path": target_path,
    }
    async with session.post(
        "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
        cookies=cookie,
        headers=headers,
        json=json_data,
    ) as response:
        data = await response.json()
    return data["data"]["biz_data"]["challenge"]


async def write_string_pow(text, alloc_func, memory, store):
    data = text.encode("utf-8")
    ptr = alloc_func(store, len(data))
    mem = memory.data_ptr(store)
    for i in range(len(data)):
        mem[ptr + i] = data[i]
    return ptr, len(data)


async def find_pow_answer(challange_data):
    return await asyncio.to_thread(_find_pow_answer_blocking, challange_data)


async def solve_create_pow(target_path, auth_token):
    pow = await create_challange_pow(target_path, auth_token)
    answer = await find_pow_answer(pow)
    json_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": pow["challenge"],
        "salt": pow["salt"],
        "answer": answer,
        "signature": pow["signature"],
        "target_path": target_path,
    }
    return base64.b64encode(json.dumps(json_data).encode()).decode()


async def fetch_chat_history(chat_id, auth_token):
    headers = get_headers(auth_token)
    cookie = await get_cookies()
    session = await get_session()
    url = (
        "https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id="
        + chat_id
    )
    async with session.get(url, cookies=cookie, headers=headers) as response:
        data = await response.json()
    return data["data"]["biz_data"]["chat_messages"]


async def create_new_chat(auth_token):
    headers = get_headers(auth_token)
    cookie = await get_cookies()
    session = await get_session()
    url = "https://chat.deepseek.com/api/v0/chat_session/create"
    async with session.post(url, cookies=cookie, headers=headers) as response:
        data = await response.json()
    return data["data"]["biz_data"]["chat_session"]["id"]


async def send_message(
    chat_id,
    auth_token,
    message,
    parent_message_id,
    thinking=False,
    search=False,
    model_type=None,
    file_ids_=[],
):
    cookie = await get_cookies()
    session = await get_session()
    if parent_message_id == 0:
        parent_message_id = None
    if model_type == "expert":
        file_ids = []
    elif model_type == "vision" and file_ids_ != []:
        headers = get_headers(auth_token)
        file_ids = []
        for i in file_ids_:
            json_data = {
                "file_id": i,
                "to_model_type": "vision",
            }
            async with session.post(
                "https://chat.deepseek.com/api/v0/file/fork_file_task",
                headers=headers,
                json=json_data,
                cookies=cookie,
            ) as resp:
                resp_json = await resp.json()
            status = resp_json["data"]["biz_data"]["status"]
            file_id = resp_json["data"]["biz_data"]["id"]
            while status in ["PENDING", "PARSING"]:
                await asyncio.sleep(0.3)
                async with session.get(
                    "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids="
                    + file_id,
                    headers=headers,
                    cookies=cookie,
                ) as resp:
                    resp_json = await resp.json()
                status = resp_json["data"]["biz_data"]["files"][0]["status"]
            file_ids.append(file_id)
    else:
        file_ids = file_ids_

    url = "https://chat.deepseek.com/api/v0/chat/completion"
    headers = get_headers(
        auth_token, await solve_create_pow("/api/v0/chat/completion", auth_token)
    )
    json_data = {
        "chat_session_id": chat_id,
        "parent_message_id": parent_message_id,
        "model_type": model_type,
        "prompt": message,
        "ref_file_ids": file_ids,
        "thinking_enabled": thinking,
        "search_enabled": search,
        "preempt": False,
        "action": None,
    }

    async with session.post(
        url, cookies=cookie, headers=headers, json=json_data
    ) as r:
        async for line in r.content:
            if not line:
                continue
            decoded_line = line.decode("utf-8").strip()
            if decoded_line.startswith("event:"):
                continue

            if decoded_line.startswith("data: "):
                json_str = decoded_line[6:]
                if "FINISHED" in json_str or "BATCH" in json_str:
                    return
                data = json.loads(json_str)

                if "v" in data:
                    if isinstance(data["v"], dict) and "response" in data["v"]:
                        fragments = data["v"]["response"].get("fragments")
                        if fragments:
                            yield fragments[0]["content"]
                    elif isinstance(data["v"], str):
                        yield data["v"]

                elif data.get("o") == "APPEND":
                    yield data.get("v", "")


async def upload_file(file_bytes, file_name, file_content_type, auth_token):
    cookie = await get_cookies()
    session = await get_session()
    url = "https://chat.deepseek.com/api/v0/file/upload_file"
    file_size = len(file_bytes)
    pow_response = await solve_create_pow("/api/v0/file/upload_file", auth_token)
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode(
            "utf-8"
        ),
        f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response)
    headers.update(
        {
            "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
            "x-file-size": str(file_size),
        }
    )
    async with session.post(
        url, data=reconstructed_body, cookies=cookie, headers=headers
    ) as response:
        resp_json = await response.json()
    file_id = resp_json["data"]["biz_data"]["id"]
    yield ("uploaded", file_id)
    js_data = resp_json["data"]["biz_data"]
    status = js_data["status"]
    headers = get_headers(auth_token)
    while status in ["PENDING", "PARSING"]:
        yield ("uploaded", file_id)
        await asyncio.sleep(0.3)
        async with session.get(
            "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
            headers=headers,
            cookies=cookie,
        ) as resp:
            js_data = (await resp.json())["data"]["biz_data"]["files"][0]
        status = js_data["status"]

    if status == "SUCCESS":
        tp_data = datetime.fromtimestamp(js_data["updated_at"], timezone.utc)
        yield (
            "success",
            {
                "file_id": file_id,
                "openai_timestamp": int(js_data["updated_at"]),
                "size": js_data["file_size"],
                "anthropic_timestamp": tp_data.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
        )
    else:
        yield ("error", file_id)


async def get_file_content(auth_token, file_id):
    cookie = await get_cookies()
    session = await get_session()
    headers = get_headers(auth_token)
    async with session.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers,
        cookies=cookie,
    ) as resp:
        resp_json = await resp.json()
    yield mimetypes.guess_type(resp_json["data"]["biz_data"]["files"][0]["file_name"])[0]
    file_path = (
        "https://files.deepseeksvc.com/api"
        + resp_json["data"]["biz_data"]["files"][0]["signed_path"]
        + "&ty=r"
    )
    async with session.get(file_path) as data:
        async for chunk in data.content.iter_chunked(8192):
            if chunk:
                yield chunk
