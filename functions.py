import asyncio
import base64
import json
import mimetypes
import os
import random
import re
import sqlite3
import string
import time
from datetime import datetime, timezone

import aiohttp
import deepseek_tokenizer
import wasmtime
from playwright.async_api import async_playwright

wasm_path = "wasm/deepseek_pow_solver.wasm"
_session = None
_db = "deeperseeker.db"


def get_db():
    conn = sqlite3.connect(_db)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tokens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            token TEXT,
            status TEXT DEFAULT 'ACTIVE'
        );
        CREATE TABLE IF NOT EXISTS sessions (
            signature TEXT PRIMARY KEY,
            token_id INTEGER,
            deepseek_session_id TEXT,
            parent_message_id INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS session_map (
            old_session TEXT PRIMARY KEY,
            new_session TEXT,
            token_id INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()


async def get_session():
    global _session
    if _session is None:
        _session = aiohttp.ClientSession()
    return _session


def get_headers(auth_token, pow=None):
    headers = {
        "accept": "*/*",
        "accept-language": "en-GB,en-US;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": "https://chat.deepseek.com",
        "priority": "u=1, i",
        "referer": "https://chat.deepseek.com/",
        "sec-ch-ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        "x-client-bundle-id": "com.deepseek.chat",
        "x-client-locale": "en_US",
        "x-client-platform": "web",
        "x-client-timezone-offset": "19800",
        "x-client-version": "2.3.0",
    }
    if auth_token:
        headers["authorization"] = f"Bearer {auth_token}"
    if pow:
        headers["x-ds-pow-response"] = pow
    return headers


async def get_cookies():
    if not os.path.exists("aws_cookies_deepseek.json"):
        await _generate_cookies()
    else:
        cookies = json.load(open("aws_cookies_deepseek.json"))
        if cookies.get("expiry") is None or cookies["expiry"] <= time.time():
            await _generate_cookies()
    cookies = json.load(open("aws_cookies_deepseek.json"))
    return cookies["cookie"]


async def _generate_cookies():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://chat.deepseek.com/", wait_until="domcontentloaded")
        await page.wait_for_selector("body")
        try:
            await page.wait_for_url("**/sign_in*", timeout=120000)
        except Exception:
            pass
        cookies = await context.cookies()
        final_cookies = {}
        expiry = None
        for i in cookies:
            if i.get("name") == "aws-waf-token":
                expiry = i["expires"]
            final_cookies[i["name"]] = i["value"]
        final_cookies["ds_cookie_preference"] = "%257B%2522level%2522%253A%2522all%2522%257D"
        if not expiry:
            pass
        await browser.close()
    with open("aws_cookies_deepseek.json", "w") as f:
        f.write(json.dumps({"cookie": final_cookies, "expiry": expiry}))


def get_auth_token():
    conn = get_db()
    row = conn.execute("SELECT token FROM tokens LIMIT 1").fetchone()
    conn.close()
    if row:
        return row[0]
    return None


def add_token(token, alias=None):
    conn = get_db()
    if not conn.execute("SELECT 1 FROM tokens WHERE id = 1").fetchone():
        next_id = 1
    else:
        row = conn.execute("""
            SELECT min(t1.id + 1)
            FROM tokens t1
            LEFT JOIN tokens t2 ON t1.id + 1 = t2.id
            WHERE t2.id IS NULL
        """).fetchone()
        next_id = row[0] if row and row[0] else 1
    conn.execute("INSERT INTO tokens (id, alias, token, status) VALUES (?, ?, ?, 'ACTIVE')", (next_id, alias, token))
    conn.commit()
    conn.close()


def get_tokens():
    conn = get_db()
    rows = conn.execute("SELECT id, alias, token, status FROM tokens").fetchall()
    conn.close()
    return [{"id": r[0], "alias": r[1], "token": r[2], "status": r[3]} for r in rows]


def get_token(token_id):
    conn = get_db()
    row = conn.execute("SELECT id, alias, token, status FROM tokens WHERE id = ?", (token_id,)).fetchone()
    conn.close()
    if row:
        return {"id": row[0], "alias": row[1], "token": row[2], "status": row[3]}
    return None


def delete_token(token_id):
    conn = get_db()
    conn.execute("DELETE FROM tokens WHERE id = ?", (token_id,))
    conn.commit()
    conn.close()


def pick_token():
    conn = get_db()
    row = conn.execute("SELECT id FROM tokens WHERE status = 'ACTIVE' ORDER BY RANDOM() LIMIT 1").fetchone()
    if row:
        conn.close()
        return row[0]
    row = conn.execute("SELECT id FROM tokens ORDER BY id LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def mark_limited(token_id):
    conn = get_db()
    conn.execute("UPDATE tokens SET status = ? WHERE id = ?", ("RATE_LIMITED", token_id))
    conn.commit()
    conn.close()


def mark_active(token_id):
    conn = get_db()
    conn.execute("UPDATE tokens SET status = ? WHERE id = ?", ("ACTIVE", token_id))
    conn.commit()
    conn.close()


def find_session(sig):
    conn = get_db()
    row = conn.execute("SELECT token_id, deepseek_session_id, parent_message_id FROM sessions WHERE signature = ?", (sig,)).fetchone()
    conn.close()
    if row:
        return {"token_id": row[0], "session_id": row[1], "parent_message_id": row[2]}
    return None


def save_session(sig, token_id, session_id, parent_message_id=0):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO sessions (signature, token_id, deepseek_session_id, parent_message_id)
           VALUES (?, ?, ?, ?)""",
        (sig, token_id, session_id, parent_message_id),
    )
    conn.commit()
    conn.close()


def save_session_map(old_session, new_session, token_id):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO session_map (old_session, new_session, token_id)
           VALUES (?, ?, ?)""",
        (old_session, new_session, token_id),
    )
    conn.commit()
    conn.close()


def get_session_map(old_session):
    conn = get_db()
    row = conn.execute("SELECT new_session, token_id FROM session_map WHERE old_session = ?", (old_session,)).fetchone()
    conn.close()
    if row:
        return {"new_session": row[0], "token_id": row[1]}
    return None


DEEPSEEK_TARIFFS = {
    "deepseek-v4-flash": {
        "cache_miss_input": 0.14,
        "cache_hit_input": 0.0028,
        "output_generation": 0.28,
    },
    "deepseek-v4-pro": {
        "cache_miss_input": 0.435,
        "cache_hit_input": 0.003625,
        "output_generation": 0.87,
    },
}


def count_tokens(text, model="deepseek-v4-flash"):
    return len(deepseek_tokenizer.ds_token.encode(text))


def normalize_tool_call(tool_data_or_name, args_if_name=None):
    if isinstance(tool_data_or_name, str):
        name = tool_data_or_name
        args = args_if_name if args_if_name is not None else {}
    elif isinstance(tool_data_or_name, dict):
        tool_data = tool_data_or_name
        if "function" in tool_data and isinstance(tool_data["function"], dict):
            fn = tool_data["function"]
            name = fn.get("name") or tool_data.get("name")
            args = fn.get("arguments") or fn.get("parameters") or fn.get("input") or fn.get("args") or fn.get("params") or {}
        else:
            name = tool_data.get("name") or tool_data.get("tool") or tool_data.get("tool_name") or tool_data.get("function") or tool_data.get("action")
            args = tool_data.get("arguments") or tool_data.get("parameters") or tool_data.get("input") or tool_data.get("args") or tool_data.get("params") or tool_data.get("tool_input") or tool_data.get("action_input") or {}
    else:
        return None

    if not name or not isinstance(name, str):
        return None
    if isinstance(args, (dict, list)):
        args_str = json.dumps(args)
    elif isinstance(args, str):
        args_str = args
        try:
            json.loads(args_str)
        except Exception:
            args_str = json.dumps(args_str)
    else:
        args_str = json.dumps({})
    call_id = "call_" + "".join(random.choices(string.ascii_letters + string.digits, k=8))
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": args_str,
        },
    }


def clean_json_str(s):
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def parse_tools(text):
    tools = []
    clean_text = text
    seen_sigs = set()

    param_names = {"command", "description", "file_path", "content", "path", "prompt", "query", "subject", "old_string", "new_string", "url", "input"}
    tool_matches = list(re.finditer(r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:tool_call|invoke|function_call)\s+(?:name|tool)=[\x27\x22]([A-Za-z0-9_]+)[\x27\x22][^>]*>", text, re.IGNORECASE))
    real_tool_matches = [m for m in tool_matches if m.group(1).lower() not in param_names]

    if real_tool_matches:
        for i, tm in enumerate(real_tool_matches):
            candidate_name = tm.group(1).strip()
            start_idx = tm.end()
            end_idx = real_tool_matches[i+1].start() if i + 1 < len(real_tool_matches) else len(text)
            body = text[start_idx:end_idx]
            args = {}
            p_matches = re.finditer(r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:parameter|tool_call|param|invoke)\s+name=[\x27\x22]([A-Za-z0-9_]+)[\x27\x22][^>]*>(.*?)(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:parameter|tool_call|param|invoke)>|(?=<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:parameter|tool_call|param|invoke)\s+name=)|$)", body, flags=re.DOTALL | re.IGNORECASE)
            for pm in p_matches:
                p_name = pm.group(1).strip()
                p_val = pm.group(2).strip()
                p_val = re.sub(r"</?[A-Za-z0-9_]+[^>]*>", "", p_val).strip()
                try:
                    args[p_name] = json.loads(p_val)
                except Exception:
                    args[p_name] = p_val
            if candidate_name and args:
                norm = normalize_tool_call(candidate_name, args)
                if norm:
                    sig = (norm["function"]["name"], norm["function"]["arguments"])
                    if sig not in seen_sigs:
                        seen_sigs.add(sig)
                        tools.append(norm)

    if tools:
        clean_text = re.sub(r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:tool_calls?|tool_call)[^>]*>.*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:tool_calls?|tool_call)>|$)", "", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()
        clean_text = re.sub(r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:invoke|function_call)[^>]*>.*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?(?:invoke|function_call)>|$)", "", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()

    if not tools and "DSML" in text:
        dsml_block_pattern = re.compile(r"<[｜\|]{2}DSML[｜\|]{2}([A-Za-z0-9_]+)>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}\1>|$)", re.DOTALL | re.IGNORECASE)
        param_pattern_b = re.compile(r"<[｜\|]{2}DSML[｜\|]{2}B([A-Za-z0-9_]+)[^>]*>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}B.*?>|$)", re.DOTALL | re.IGNORECASE)
        for m in dsml_block_pattern.finditer(text):
            tool_name = m.group(1).strip()
            body = m.group(2)
            args = {}
            for pm in param_pattern_b.finditer(body):
                p_name = pm.group(1).lower().strip()
                p_val = pm.group(2).strip()
                try:
                    args[p_name] = json.loads(p_val)
                except Exception:
                    args[p_name] = p_val
            norm = normalize_tool_call(tool_name, args)
            if norm:
                sig = (norm["function"]["name"], norm["function"]["arguments"])
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    tools.append(norm)
        if not tools:
            tool_match = re.search(r"[｜\|]{2}DSML[｜\|]{2}(Bash|Read|Write|Edit|Agent|TaskList|TaskCreate|WebSearch|[A-Za-z0-9_]+)", text, re.IGNORECASE)
            if tool_match:
                candidate = tool_match.group(1).strip()
                tool_name = "Bash" if candidate.lower().startswith("b") and candidate.lower() not in ["bdescription", "bparam"] else candidate
                args = {}
                cmd_match = re.search(r"[｜\|]{2}B[\x22\x27]?command[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)", text, re.DOTALL | re.IGNORECASE)
                desc_match = re.search(r"[｜\|]{2}B[\x22\x27]?description[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)", text, re.DOTALL | re.IGNORECASE)
                if cmd_match:
                    clean_cmd = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", cmd_match.group(1)).strip("\x22\x27() ")
                    args["command"] = clean_cmd
                if desc_match:
                    clean_desc = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", desc_match.group(1)).strip("\x22\x27() ")
                    args["description"] = clean_desc
                norm = normalize_tool_call(tool_name, args)
                if norm:
                    sig = (norm["function"]["name"], norm["function"]["arguments"])
                    if sig not in seen_sigs:
                        seen_sigs.add(sig)
                        tools.append(norm)
        if tools:
            clean_text = re.sub(r"<[｜\|]{2}DSML[｜\|]{2}[^>]*>.*?(?:</[｜\|]{2}DSML[｜\|]{2}[^>]*>|$)", "", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()
            clean_text = re.sub(r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()

    if not tools:
        fn_call_pattern = re.compile(r"<function_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</function_call>", re.DOTALL | re.IGNORECASE)
        for m in fn_call_pattern.finditer(text):
            name = m.group(1).strip()
            args_raw = m.group(2).strip()
            try:
                args = json.loads(args_raw)
            except Exception:
                args = args_raw
            norm = normalize_tool_call(name, args)
            if norm:
                sig = (norm["function"]["name"], norm["function"]["arguments"])
                if sig not in seen_sigs:
                    seen_sigs.add(sig)
                    tools.append(norm)
        if tools:
            clean_text = re.sub(r"<function_call>.*?</function_call>", "", clean_text, flags=re.DOTALL | re.IGNORECASE).strip()

    if not tools:
        tag_regex = re.compile(r"<(?:tool_call|function_call)(?:\s+(?:name|tool|function)=[\x27\x22]([^\x27\x22]+)[\x27\x22])?\s*>", re.IGNORECASE)
        decoder = json.JSONDecoder()
        matches = list(tag_regex.finditer(text))
        if matches:
            for m in matches:
                tag_name = m.group(1)
                after_tag = text[m.end():]
                brace_pos = after_tag.find("{")
                if brace_pos != -1:
                    json_substr = after_tag[brace_pos:]
                    data = None
                    try:
                        data, _ = decoder.raw_decode(json_substr)
                    except Exception:
                        pass
                    if not data:
                        cleaned_json = re.sub(r"</?(?:tool_call|function_call|tool_calls|invoke)[^>]*>.*", "", json_substr, flags=re.DOTALL).strip()
                        open_b = cleaned_json.count("{")
                        close_b = cleaned_json.count("}")
                        if open_b > close_b:
                            cleaned_json += "}" * (open_b - close_b)
                        try:
                            data = json.loads(cleaned_json)
                        except Exception:
                            pass
                    if isinstance(data, dict):
                        name = tag_name or data.get("name") or data.get("tool") or data.get("tool_name") or data.get("function") or data.get("action")
                        args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or data.get("params") or data.get("tool_input") or data.get("action_input")
                        if args is None:
                            if tag_name:
                                args = {k: v for k, v in data.items() if k not in ["name", "tool", "function"]}
                            else:
                                args = {}
                        if name:
                            norm = normalize_tool_call(name, args)
                            if norm:
                                sig = (norm["function"]["name"], norm["function"]["arguments"])
                                if sig not in seen_sigs:
                                    seen_sigs.add(sig)
                                    tools.append(norm)
            clean_text = re.sub(r"<(?:tool_call|function_call)[^>]*>.*?(?:</(?:tool_call|function_call)>|$)", "", text, flags=re.DOTALL).strip()

    if not tools:
        codeblock_pattern = r"```(?:tool_call|function_call)\s*(.*?)\s*```"
        cb_matches = list(re.finditer(codeblock_pattern, clean_text, flags=re.DOTALL))
        for m in cb_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict):
                    name = data.get("name") or data.get("tool") or data.get("function") or data.get("action")
                    args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or {}
                    norm = normalize_tool_call(name, args)
                    if norm:
                        sig = (norm["function"]["name"], norm["function"]["arguments"])
                        if sig not in seen_sigs:
                            seen_sigs.add(sig)
                            tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(codeblock_pattern, "", clean_text, flags=re.DOTALL).strip()

    if not tools:
        json_pattern = r"```json\s*(\{.*?\})\s*```"
        json_matches = list(re.finditer(json_pattern, clean_text, flags=re.DOTALL))
        for m in json_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and ("name" in data or "tool" in data or "function" in data):
                    name = data.get("name") or data.get("tool") or data.get("function") or data.get("action")
                    args = data.get("arguments") or data.get("parameters") or data.get("input") or data.get("args") or {}
                    norm = normalize_tool_call(name, args)
                    if norm:
                        sig = (norm["function"]["name"], norm["function"]["arguments"])
                        if sig not in seen_sigs:
                            seen_sigs.add(sig)
                            tools.append(norm)
            except Exception:
                pass
    if tools:
        clean_text = ""
    else:
        clean_text = re.sub(r"</?(?:tool_calls?|invoke|function_call|parameter)[^>]*>", "", clean_text, flags=re.IGNORECASE).strip()
    return tools, clean_text


class StreamToolParser:
    def __init__(self):
        self.buffer = ""
        self.in_tool = False
        self.has_tool = False

    def feed(self, chunk):
        self.buffer += chunk
        results = []
        while True:
            if self.in_tool:
                end_tags = ["</tool_call>", "</function_call>", "</invoke>", "</tool_calls>", "</｜｜DSML｜｜", "</||DSML||"]
                end_pos = -1
                end_tag_len = 0
                for tag in end_tags:
                    if tag in self.buffer:
                        idx = self.buffer.index(tag)
                        if end_pos == -1 or idx < end_pos:
                            end_pos = idx
                            end_tag_len = len(tag)
                if end_pos != -1:
                    idx = end_pos + end_tag_len
                    tool_xml = self.buffer[:idx]
                    self.buffer = self.buffer[idx:]
                    self.in_tool = False
                    self.has_tool = True
                    parsed, _ = parse_tools(tool_xml)
                    for item in parsed:
                        results.append({"tool": item})
                else:
                    brace_idx = self.buffer.find("{")
                    if brace_idx != -1:
                        decoder = json.JSONDecoder()
                        try:
                            data, end_pos = decoder.raw_decode(self.buffer[brace_idx:])
                            norm = normalize_tool_call(data)
                            if norm:
                                results.append({"tool": norm})
                                self.buffer = self.buffer[brace_idx + end_pos:]
                                self.in_tool = False
                                self.has_tool = True
                        except Exception:
                            break
                    else:
                        break
            else:
                start_tags = ["<tool_call", "<function_call", "<invoke", "<tool_calls", "<｜｜DSML｜｜", "<||DSML||", "DSML"]
                start_pos = -1
                for tag in start_tags:
                    if tag in self.buffer:
                        idx = self.buffer.index(tag)
                        if start_pos == -1 or idx < start_pos:
                            start_pos = idx
                if start_pos != -1:
                    before = self.buffer[:start_pos]
                    self.buffer = self.buffer[start_pos:]
                    self.in_tool = True
                    self.has_tool = True
                else:
                    if len(self.buffer) > 30 and not any(self.buffer.endswith(tag[:i]) for tag in ["<tool_call", "<function_call", "<invoke", "<tool_calls", "<｜｜DSML｜｜", "<||DSML||", "DSML"] for i in range(1, len(tag))):
                        if not self.has_tool:
                            results.append({"text": self.buffer})
                        self.buffer = ""
                    else:
                        break
        return results

    def flush(self):
        if self.buffer and not self.has_tool and not self.in_tool:
            result = [{"text": self.buffer}]
            self.buffer = ""
            return result
        self.buffer = ""
        return []


def summarize_messages(messages, max_tokens=500):
    recent = messages[-10:] if len(messages) > 10 else messages
    parts = []
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
        if content:
            parts.append(f"{role}: {content[:200]}")
    summary = "\n".join(parts)
    tokens = count_tokens(summary)
    while tokens > max_tokens and len(parts) > 1:
        parts = parts[1:]
        summary = "\n".join(parts)
        tokens = count_tokens(summary)
    return summary


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
    ch_ptr, ch_len = asyncio.run(write_string_pow(challange_data["challenge"], alloc_func, memory, store))
    salt_ptr, salt_len = asyncio.run(write_string_pow(challange_data["salt"], alloc_func, memory, store))
    result = solve_func(store, ch_ptr, ch_len, salt_ptr, salt_len, challange_data["expire_at"], challange_data["difficulty"])
    if result < 0:
        result = result + 0x10000000000000000
    return result if result != 0xFFFFFFFFFFFFFFFF else None


async def create_challange_pow(target_path, auth_token):
    headers = get_headers(auth_token)
    cookie = await get_cookies()
    session = await get_session()
    async with session.post(
        "https://chat.deepseek.com/api/v0/chat/create_pow_challenge",
        cookies=cookie, headers=headers, json={"target_path": target_path},
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
    url = "https://chat.deepseek.com/api/v0/chat/history_messages?chat_session_id=" + chat_id
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


async def send_message(chat_id, auth_token, message, parent_message_id, thinking=False, search=False, model_type=None, file_ids_=None):
    if file_ids_ is None:
        file_ids_ = []
    cookie = await get_cookies()
    session = await get_session()
    if parent_message_id == 0:
        parent_message_id = None
    if model_type == "expert":
        file_ids = []
    elif model_type == "vision" and file_ids_:
        headers = get_headers(auth_token)
        file_ids = []
        for i in file_ids_:
            async with session.post(
                "https://chat.deepseek.com/api/v0/file/fork_file_task",
                headers=headers, json={"file_id": i, "to_model_type": "vision"}, cookies=cookie,
            ) as resp:
                resp_json = await resp.json()
            status = resp_json["data"]["biz_data"]["status"]
            file_id = resp_json["data"]["biz_data"]["id"]
            while status in ["PENDING", "PARSING"]:
                await asyncio.sleep(0.3)
                async with session.get(
                    "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
                    headers=headers, cookies=cookie,
                ) as resp:
                    resp_json = await resp.json()
                status = resp_json["data"]["biz_data"]["files"][0]["status"]
            file_ids.append(file_id)
    else:
        file_ids = file_ids_

    url = "https://chat.deepseek.com/api/v0/chat/completion"
    headers = get_headers(auth_token, await solve_create_pow("/api/v0/chat/completion", auth_token))
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

    async with session.post(url, cookies=cookie, headers=headers, json=json_data) as r:
        if r.status != 200:
            error_text = await r.text()
            raise Exception(f"HTTP {r.status}: {error_text}")
        
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
                if "v" in data and isinstance(data["v"], dict) and "response" in data["v"]:
                    fragments = data["v"]["response"].get("fragments")
                    if fragments:
                        fragment = fragments[0]
                        if fragment.get("type") == "THINK":
                            yield "<think>\n" + fragment.get("content", "")
                        elif fragment.get("type") == "RESPONSE":
                            yield fragment.get("content", "")
                        else:
                            yield fragment.get("content", "")
                    continue
                
                if data.get("p") == "response/fragments" and data.get("o") == "APPEND":
                    fragments = data.get("v")
                    if isinstance(fragments, list) and len(fragments) > 0:
                        fragment = fragments[0]
                        if fragment.get("type") == "RESPONSE":
                            yield "\n</think>\n\n" + fragment.get("content", "")
                        elif fragment.get("type") == "THINK":
                            yield "\n<think>\n" + fragment.get("content", "")
                        else:
                            yield fragment.get("content", "")
                    continue

                if "v" in data:
                    if isinstance(data["v"], str):
                        yield data["v"]
                elif data.get("o") == "APPEND" and isinstance(data.get("v"), str):
                    yield data["v"]


async def upload_file(file_bytes, file_name, file_content_type, auth_token):
    cookie = await get_cookies()
    session = await get_session()
    url = "https://chat.deepseek.com/api/v0/file/upload_file"
    file_size = len(file_bytes)
    pow_response = await solve_create_pow("/api/v0/file/upload_file", auth_token)
    boundary = b"----WebKitFormBoundaryTB0pXOQR2RL219Hu"
    body_parts = [
        b"--" + boundary + b"\r\n",
        f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode("utf-8"),
        f"Content-Type: {file_content_type}\r\n\r\n".encode("utf-8"),
        file_bytes,
        b"\r\n--" + boundary + b"--\r\n",
    ]
    reconstructed_body = b"".join(body_parts)
    headers = get_headers(auth_token, pow_response)
    headers.update({
        "content-type": f"multipart/form-data; boundary={boundary.decode('utf-8')}",
        "x-file-size": str(file_size),
    })
    async with session.post(url, data=reconstructed_body, cookies=cookie, headers=headers) as response:
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
            headers=headers, cookies=cookie,
        ) as resp:
            js_data = (await resp.json())["data"]["biz_data"]["files"][0]
        status = js_data["status"]
    if status == "SUCCESS":
        tp_data = datetime.fromtimestamp(js_data["updated_at"], timezone.utc)
        yield ("success", {
            "file_id": file_id,
            "openai_timestamp": int(js_data["updated_at"]),
            "size": js_data["file_size"],
            "anthropic_timestamp": tp_data.strftime("%Y-%m-%dT%H:%M:%SZ"),
        })
    else:
        yield ("error", file_id)


async def get_file_content(auth_token, file_id):
    cookie = await get_cookies()
    session = await get_session()
    headers = get_headers(auth_token)
    async with session.get(
        "https://chat.deepseek.com/api/v0/file/fetch_files?file_ids=" + file_id,
        headers=headers, cookies=cookie,
    ) as resp:
        resp_json = await resp.json()
    yield mimetypes.guess_type(resp_json["data"]["biz_data"]["files"][0]["file_name"])[0]
    file_path = "https://files.deepseeksvc.com/api" + resp_json["data"]["biz_data"]["files"][0]["signed_path"] + "&ty=r"
    async with session.get(file_path) as data:
        async for chunk in data.content.iter_chunked(8192):
            if chunk:
                yield chunk
