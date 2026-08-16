import asyncio
import base64
import hashlib
import json
import mimetypes
import random
import re
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


async def extract_and_upload_files(messages, auth_token):
    result_fileids = []
    for idx, i in enumerate(messages):
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
                    url_path = urlsplit(j["image_url"]["url"]).path
                    filename = Path(url_path).name
                    mime_type, _ = mimetypes.guess_type(filename)
                    session = await get_session()
                    async with session.get(j["image_url"]["url"]) as resp:
                        file_bytes = await resp.read()

                    async for k in upload_file(file_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
                        elif k[0] == "error":
                            result_fileids.append(k[1])

                else:
                    mimetype_base, base64_data = j["image_url"]["url"].split(",")
                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    filename = (
                        "inline_uploaded_"
                        + str(uuid.uuid4())
                        + mimetypes.guess_extension(mime_type)
                    )
                    data_bytes = base64.b64decode(
                        (base64_data.split("data:")[1] if "data:" in base64_data else base64_data).encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
                        elif k[0] == "error":
                            result_fileids.append(k[1])
            elif j["type"] == "file":
                if "file_id" in j["file"]:
                    result_fileids.append(j["file_id"])
                if "file_data" in j["file"]:
                    filename = j["file"]["filename"]
                    mimetype_base, base64_data = j["file_data"].split(",")

                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    data_bytes = base64.b64decode(
                        (base64_data.split("data:")[1] if "data:" in base64_data else base64_data).encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
                        elif k[0] == "error":
                            result_fileids.append(k[1])
            elif j["type"] == "document" or j["type"] == "image":
                if j["source"]["type"] == "base64":
                    base64_data = j["source"]["data"].split(",")[1] if "," in j["source"]["data"] else j["source"]["data"]
                    mime_type = j["source"]["media_type"]
                    filename = (
                        "inline_uploaded_"
                        + str(uuid.uuid4())
                        + mimetypes.guess_extension(mime_type)
                    )
                    data_bytes = base64.b64decode(base64_data.encode())
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1]["file_id"])
                        elif k[0] == "error":
                            result_fileids.append(k[1])
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
                tc_json = json.dumps({"name": name, "arguments": args}, sort_keys=True)
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
                        tc_json = json.dumps({"name": c.get("name"), "arguments": args}, sort_keys=True)
                        parts.append("<tool_call>" + tc_json + "</tool_call>")
                    elif c.get("type") == "tool_result":
                        res_content = c.get("content", "")
                        if isinstance(res_content, list):
                            res_content = " ".join(item.get("text", "") for item in res_content if isinstance(item, dict) and item.get("type") == "text")
                        tool_id = c.get("tool_use_id", "tool")
                        parts.append("[Tool Result for " + str(tool_id) + "]: " + str(res_content))
            content = "\n".join(parts)
            
        canon.append({"role": role, "content": content})
    return canon


async def generate_signature(messages, model):
    last_ast_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "assistant":
            last_ast_idx = i
            break

    if last_ast_idx == -1:
        history = messages
    else:
        history = messages[:last_ast_idx + 1]

    canon_history = canonicalize_messages(history)
    dump = json.dumps(canon_history, sort_keys=True)
    return hashlib.sha256(f"{model}_{dump}".encode("utf-8")).hexdigest()


async def build_prompt(messages, tools, model, is_first_message=False):
    final_prompt = ""
    tools_extract = await extract_tools(tools)
    tool_instructions = (
        "IMPORTANT EXECUTION DIRECTIVE:\n"
        "You are an AI agent with tools. When an actionable request is given (such as listing/reading/editing files, executing commands, searching, or computing), you MUST execute the appropriate tool call immediately or even when you feel user wants you or you think you should call tools then don't delay just call them, think which tool to use then use below format to call it simply.\n"
"You have complete right to call tools anytime you feel you should or user is asking you to. Don't wait for user to explicitly say you to tool call use your own thinking to understand where to and where not to call it."
        "DO NOT say ambigious messages or provide conversational text/promises. Output ONLY the tool call XML block in this exact schema with no surrounding text or markdown code blocks:\n"
        "<tool_call>{\"name\": \"exact_tool_name\", \"arguments\": {\"param\": \"value\"}}</tool_call>\n"
        "Never use keys like 'parameters', 'tool_name', 'action', or 'function'. Output strictly 'name' and 'arguments'."
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
        if tools_extract:
            final_prompt += tool_instructions + "\n"
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

    return final_prompt


async def parse_tool_call(tools_list):
    tools_called = []
    for i in tools_list:
        i = i.strip()
        cleaned = i.replace("<tool_call>", "").replace("</tool_call>", "").replace("<function_call>", "").replace("</function_call>", "").strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        tool_json = json.loads(cleaned.strip())
        characters = string.ascii_letters + string.digits
        call_id = "call_" + "".join(random.choices(characters, k=8))
        if "function" in tool_json and isinstance(tool_json["function"], dict):
            tool_name = tool_json["function"].get("name") or tool_json.get("name")
            tool_args = tool_json["function"].get("arguments") or tool_json["function"].get("parameters") or tool_json.get("arguments") or {}
        else:
            tool_name = tool_json.get("name") or tool_json.get("tool") or tool_json.get("tool_name") or tool_json.get("function") or tool_json.get("action")
            tool_args = tool_json.get("arguments") or tool_json.get("parameters") or tool_json.get("input") or tool_json.get("args") or tool_json.get("params") or tool_json.get("tool_input") or tool_json.get("action_input") or {}
        if isinstance(tool_args, (dict, list)):
            tool_args = json.dumps(tool_args)
        elif isinstance(tool_args, str):
            try:
                json.loads(tool_args)
            except (json.JSONDecodeError, TypeError):
                tool_args = json.dumps(tool_args)
        if tool_name not in ["computer_use", "bash", "text_editor"]:
            tools_called.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": tool_name, "arguments": tool_args},
                }
            )
        else:
            tools_called.append(
                {
                    "id": call_id,
                    "type": tool_name,
                    "function": {"name": tool_name, "arguments": tool_args},
                }
            )
    return tools_called


async def parse_tool_call_streaming(tool_txt):
    tool_txt = tool_txt.strip()
    cleaned = tool_txt.replace("<tool_call>", "").replace("</tool_call>", "").replace("<function_call>", "").replace("</function_call>", "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    tool_json = json.loads(cleaned.strip())
    characters = string.ascii_letters + string.digits
    call_id = "call_" + "".join(random.choices(characters, k=8))
    if "function" in tool_json and isinstance(tool_json["function"], dict):
        tool_name = tool_json["function"].get("name") or tool_json.get("name")
        tool_args = tool_json["function"].get("arguments") or tool_json["function"].get("parameters") or tool_json.get("arguments") or {}
    else:
        tool_name = tool_json.get("name") or tool_json.get("tool") or tool_json.get("tool_name") or tool_json.get("function") or tool_json.get("action")
        tool_args = tool_json.get("arguments") or tool_json.get("parameters") or tool_json.get("input") or tool_json.get("args") or tool_json.get("params") or tool_json.get("tool_input") or tool_json.get("action_input") or {}
    if isinstance(tool_args, (dict, list)):
        tool_args = json.dumps(tool_args)
    elif isinstance(tool_args, str):
        try:
            json.loads(tool_args)
        except (json.JSONDecodeError, TypeError):
            tool_args = json.dumps(tool_args)
    if tool_name not in ["computer_use", "bash", "text_editor"]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": tool_args},
        }
    else:
        return {
            "id": call_id,
            "type": tool_name,
            "function": {"name": tool_name, "arguments": tool_args},
        }
