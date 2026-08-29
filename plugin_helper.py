import asyncio
import base64
import hashlib
import ipaddress
import json
import mimetypes
import random
import re
import socket
import string
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from functions import get_session, upload_file


async def extract_system(messages):
    for i in messages:
        if i.get("role") == "system":
            return i.get("content")
    return None


async def extract_tools(tools):
    if not tools:
        return None
    final_tools = []
    for i in tools:
        if i.get("type") == "function":
            fn = i.get("function", {})
            name = fn.get("name", "")
            desc = fn.get("description", "")
            params = fn.get("parameters", {})
            final_tools.append(f"Tool: {name}\nDescription: {desc}\nParameters: {json.dumps(params)}")
        elif "name" in i:
            name = i.get("name", "")
            desc = i.get("description", "")
            params = i.get("input_schema", i.get("parameters", {}))
            final_tools.append(f"Tool: {name}\nDescription: {desc}\nParameters: {json.dumps(params)}")
        elif i.get("type") in ["computer_use", "text_editor", "bash"]:
            final_tools.append(f"Tool: {i['type']}\nDescription: {json.dumps(i)}")
        else:
            final_tools.append(f"Tool: {json.dumps(i)}")
    return "\n\n".join(final_tools) if final_tools else None


async def extract_tool_results(messages, latest_only=False):
    target_messages = messages
    if latest_only:
        last_ast_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "assistant":
                last_ast_idx = idx
                break
        if last_ast_idx != -1:
            target_messages = messages[last_ast_idx + 1:]
    tools_final = []
    for i in target_messages:
        if i.get("role") == "tool":
            name = i.get("name", "tool")
            call_id = i.get("tool_call_id", "")
            content = i.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            tools_final.append(f"Tool: {name} (Call ID: {call_id})\nResult: {content}")
    return "\n\n".join(tools_final) if tools_final else None


def _assert_public_url(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("unsupported url")
    infos = socket.getaddrinfo(parts.hostname, None)
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("url resolves to non-public address")


async def extract_and_upload_files(messages, auth_token, last_user_only=False):
    result_fileids = []
    scan = messages
    if last_user_only:
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user":
                scan = messages[idx:]
                break
    for idx, i in enumerate(scan):
        content = i.get("content")
        if not content:
            continue
        if isinstance(content, str):
            continue
        for j_idx, j in enumerate(content):
            if j["type"] == "text":
                continue
            elif j["type"] == "image_url":
                if j["image_url"]["url"].startswith("http"):
                    _assert_public_url(j["image_url"]["url"])
                    url_path = urlsplit(j["image_url"]["url"]).path
                    filename = Path(url_path).name
                    mime_type, _ = mimetypes.guess_type(filename)
                    session = await get_session()
                    async with session.get(j["image_url"]["url"]) as resp:
                        file_bytes = await resp.content.read(20 * 1024 * 1024 + 1)
                    if len(file_bytes) > 20 * 1024 * 1024:
                        continue

                    async for k in upload_file(file_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])

                else:
                    url_parts = j["image_url"]["url"].split(",", 1)
                    if len(url_parts) != 2:
                        continue
                    mimetype_base, base64_data = url_parts
                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    filename = (
                        "inline_uploaded_"
                        + str(uuid.uuid4())
                        + (mimetypes.guess_extension(mime_type) or ".bin")
                    )
                    data_bytes = base64.b64decode(
                        (base64_data.split("data:")[1] if "data:" in base64_data else base64_data).encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
            elif j["type"] == "file":
                if "file_id" in j["file"]:
                    result_fileids.append(j["file"]["file_id"])
                if "file_data" in j["file"]:
                    filename = j["file"]["filename"]
                    data_parts = j["file_data"].split(",", 1)
                    if len(data_parts) != 2:
                        continue
                    mimetype_base, base64_data = data_parts

                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    data_bytes = base64.b64decode(
                        (base64_data.split("data:")[1] if "data:" in base64_data else base64_data).encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
            elif j["type"] == "document" or j["type"] == "image":
                if j["source"]["type"] == "base64":
                    base64_data = j["source"]["data"].split(",")[1] if "," in j["source"]["data"] else j["source"]["data"]
                    mime_type = j["source"]["media_type"]
                    filename = (
                        "inline_uploaded_"
                        + str(uuid.uuid4())
                        + (mimetypes.guess_extension(mime_type) or ".bin")
                    )
                    data_bytes = base64.b64decode(base64_data.encode())
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
                elif j["source"]["type"] == "file":
                    result_fileids.append(j["source"]["file_id"])
    return result_fileids


async def extract_user_msg(messages):
    for i in messages[::-1]:
        if i.get("role") == "user":
            content = i.get("content")
            if isinstance(content, str):
                return content
            elif isinstance(content, list):
                parts = []
                for j in content:
                    if isinstance(j, dict) and j.get("type") == "text":
                        parts.append(j.get("text", ""))
                if parts:
                    return "\n".join(parts)
    return ""


def canonicalize_messages(messages):
    canon = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls")
        
        if tool_calls and isinstance(tool_calls, list):
            tc_parts = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name") or tc.get("name")
                args = fn.get("arguments") or tc.get("arguments")
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except Exception:
                        pass
                tc_json = json.dumps({"arguments": args, "name": name}, sort_keys=True)
                tc_parts.append("<tool_call>" + tc_json + "</tool_call>")
            content = "\n".join(tc_parts)
        elif isinstance(content, list):
            parts = []
            for c in content:
                if isinstance(c, dict):
                    if c.get("type") == "text":
                        parts.append(c.get("text", ""))
                    elif c.get("type") == "tool_use":
                        args = c.get("input", {})
                        tc_json = json.dumps({"arguments": args, "name": c.get("name")}, sort_keys=True)
                        parts.append("<tool_call>" + tc_json + "</tool_call>")
                    elif c.get("type") == "tool_result":
                        res_content = c.get("content", "")
                        if isinstance(res_content, list):
                            res_content = " ".join(item.get("text", "") for item in res_content if isinstance(item, dict) and item.get("type") == "text")
                        tool_id = c.get("tool_use_id", "tool")
                        parts.append("[Tool Result for " + str(tool_id) + "]: " + str(res_content))
            content = "\n".join(parts)
        elif isinstance(content, str):
            content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            def repl_tc(match):
                raw_json = match.group(1).strip()
                try:
                    d = json.loads(raw_json)
                    d_name = d.get("name")
                    d_args = d.get("arguments", {})
                    return "<tool_call>" + json.dumps({"arguments": d_args, "name": d_name}, sort_keys=True) + "</tool_call>"
                except Exception:
                    return match.group(0)
            content = re.sub(r"<tool_call>(.*?)</tool_call>", repl_tc, content, flags=re.DOTALL)
            
        canon.append({"role": role, "content": str(content).strip()})
    return canon


def generate_signature_sync(messages, model, scope=""):
    last_ast_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_ast_idx = i
            break

    history = messages if last_ast_idx == -1 else messages[:last_ast_idx + 1]
    canon_history = canonicalize_messages(history)
    dump = json.dumps(canon_history, sort_keys=True)
    return hashlib.sha256(f"{model}_{scope}_{dump}".encode("utf-8")).hexdigest()


async def generate_signature(messages, model, scope=""):
    return generate_signature_sync(messages, model, scope)


async def build_prompt(messages, tools, model, is_first_message=False):
    final_prompt = ""
    tools_extract = await extract_tools(tools)
    tool_instructions = (
        "TOOL USE INSTRUCTIONS:\n"
        "You have access to tools. When you need to call a tool, output ONLY the tool call XML block and nothing else:\n"
        "<tool_call>{\"name\": \"tool_name\", \"arguments\": {\"param\": \"value\"}}</tool_call>\n"
        "Never repeat past messages, history, or XML tags. Output exactly one tool call block when invoking a tool."
    )
    if is_first_message:
        if tools_extract:
            final_prompt += f"[TOOLS]\n{tools_extract}\n\n"
        system_prompt = await extract_system(messages)
        if system_prompt:
            if tools_extract:
                system_prompt += "\n\n" + tool_instructions
            final_prompt += f"[SYSTEM]\n{system_prompt}\n\n"
        elif tools_extract:
            final_prompt += f"[SYSTEM]\n{tool_instructions}\n\n"

        if len(messages) > 1:
            history_parts = []
            for msg in messages[:-1]:
                role = msg.get("role", "unknown")
                if role == "system":
                    continue
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
                if content:
                    history_parts.append(f"{role.upper()}: {content}")
            if history_parts:
                history_text = "\n".join(history_parts)
                final_prompt += f"[PREVIOUS CONVERSATION HISTORY]\n{history_text}\n\n"

        tools_result_extract = await extract_tool_results(messages, latest_only=False)
        if tools_result_extract:
            final_prompt += f"[TOOL RESULTS]\n{tools_result_extract}\n\n"

        user_msg = await extract_user_msg(messages)
        if user_msg:
            final_prompt += f"[USER]\n{user_msg}\n\n"
    else:
        last_ast_idx = -1
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "assistant":
                last_ast_idx = idx
                break

        trailing_messages = messages[last_ast_idx + 1:] if last_ast_idx != -1 else [messages[-1]]
        tools_result_extract = await extract_tool_results(messages, latest_only=True)
        if tools_result_extract:
            final_prompt += f"[TOOL RESULTS]\n{tools_result_extract}\n\n"

        trailing_user_parts = []
        for m in trailing_messages:
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str) and c:
                    trailing_user_parts.append(c)
                elif isinstance(c, list):
                    txt = " ".join(part.get("text", "") for part in c if isinstance(part, dict) and part.get("type") == "text")
                    if txt:
                        trailing_user_parts.append(txt)

        if trailing_user_parts:
            final_prompt += f"[USER]\n{chr(10).join(trailing_user_parts)}\n\n"
        elif not tools_result_extract:
            user_msg = await extract_user_msg(messages)
            if user_msg:
                final_prompt += f"[USER]\n{user_msg}\n\n"

        if tools_extract:
            final_prompt += tool_instructions + "\n"

    return final_prompt
