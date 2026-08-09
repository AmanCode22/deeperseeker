import asyncio
import base64
import hashlib
import json
import mimetypes
import random
import string
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from functions import get_session, upload_file


async def extract_system(messages):
    for i in messages:
        if i["role"] == "system":
            return i["content"]
    return None


async def extract_tools(tools):
    final_tools = ""
    for i in tools:
        if i["type"] == "function":
            final_tools += (
                "Tool Type: function. Tool name: "
                + i["function"]["name"]
                + ", description: "
                + i["function"].get("description")
                + ". Params needed: "
                + json.dumps(i["function"].get("parameters"))
                + "\n"
            )

        elif i["type"] == "computer_use":
            final_tools += (
                "Tool name: computer_use. Description: Use this to interact with the screen, mouse, and keyboard. Other whole info: "
                + json.dumps(i)
                + "\n"
            )
        elif i["type"] == "text_editor":
            final_tools += "Tool name: text_editor. Description: Use this to view and edit text files\n"
        elif i["type"] == "bash":
            final_tools += (
                "Tool name: bash. Description: Use this to run bash commands\n"
            )
        else:
            final_tools += "Tool: " + json.dumps(i)
    return final_tools if final_tools != "" else None


async def extract_tool_results(messages):
    tools_final = ""
    for i in messages:
        if i["role"] == "tool":
            tools_final += f"""Tool result for {i.get("name", "tool")}:\n{i["content"]}. Tool Call ID: {i["tool_call_id"]}\n"""
    return tools_final if tools_final != "" else None


async def extract_and_upload_files(messages, auth_token):
    result_fileids = []
    for i in messages:
        content = i.get("content")
        if not content:
            continue
        if isinstance(content, str):
            continue
        for j in content:
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
                            result_fileids.append(k[1])
                        else:
                            print("Error while uploading file.")

                else:
                    mimetype_base, base64_data = j["image_url"]["url"].split(",")
                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    filename = (
                        "inline_uploaded_"
                        + str(uuid.uuid4())
                        + mimetypes.guess_extension(mime_type)
                    )
                    data_bytes = base64.b64decode(
                        base64_data.split("data:")[1].encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1])
                        else:
                            print("Error while uploading file.")
            elif j["type"] == "file":
                if "file_id" in j["file"]:
                    result_fileids.append(j["file_id"])
                if "file_data" in j["file"]:
                    filename = j["file"]["filename"]
                    mimetype_base, base64_data = j["file_data"].split(",")

                    mime_type = mimetype_base.split(":")[1].split(";")[0]
                    data_bytes = base64.b64decode(
                        base64_data.split("data:")[1].encode()
                    )
                    async for k in upload_file(data_bytes, filename, mime_type, auth_token):
                        if k[0] == "uploaded":
                            continue
                        elif k[0] == "success":
                            result_fileids.append(k[1])
                        else:
                            print("Error while uploading file.")
            elif j["type"] == "document" or j["type"] == "image":
                if j["source"]["type"] == "base64":
                    base64_data = j["source"]["data"].split(",")[1]
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
                            result_fileids.append(k[1])
                        else:
                            print("Error while uploading file.")
                elif j["source"]["type"] == "file":
                    result_fileids.append(j["source"]["file_id"])
    return result_fileids


async def extract_user_msg(messages):
    for i in messages[::-1]:
        if i["role"] == "user":
            if isinstance(i["content"], str):
                return i["content"]
            elif isinstance(i["content"], list):
                parts = []
                for j in i["content"]:
                    if isinstance(j, dict) and j.get("type") == "text":
                        parts.append(j.get("text", ""))
                if parts:
                    return "\n".join(parts)
    return ""


async def generate_signature(messages):
    system_prompt = await extract_system(messages)
    first_user = ""
    for i in messages:
        if i["role"] == "user":
            if isinstance(i["content"], str):
                first_user = i["content"]
            else:
                for j in i["content"]:
                    if j["type"] == "text":
                        first_user = j["text"]
            break
    first_assistant = ""
    for i in messages:
        if i["role"] == "assistant":
            if isinstance(i["content"], str):
                first_assistant = i["content"]
            else:
                for j in i["content"]:
                    if j["type"] == "text":
                        first_assistant = j["text"]
            break
    joined = (
        f"{system_prompt}\n---\n{first_user}\n---\n{first_assistant}"
    )
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


async def build_prompt(messages, tools, model, is_first_message=False):
    final_prompt = ""
    tools_extract = await extract_tools(tools)
    if tools_extract:
        final_prompt += f"""[TOOLS]\n{tools_extract}\n\n"""
    if is_first_message:
        system_prompt = await extract_system(messages)
        if system_prompt:
            system_prompt += "\n If you need to call xml tags that are used for tool calls, then do not use markdown markers like code blocks around you."
            final_prompt += f"[SYSTEM]\n{system_prompt}\n\n"
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
    tools_result_extract = await extract_tool_results(messages)
    if tools_result_extract:
        final_prompt += f"""[TOOL RESULTS]\n{tools_result_extract}\n\n"""
    final_prompt += f"""[USER]\n{await extract_user_msg(messages)}\n\n"""
    if tools_extract:
        final_prompt += """IMPORTANT FOR TOOL CALLS: If you decide to call a tool, output ONLY the tool call XML block tag without any extra conversational text or markdown codeblock markers:
<tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call>\n"""
    return final_prompt


async def parse_tool_call(tools_list):
    tools_called = []
    for i in tools_list:
        i = i.strip()
        tool_json = json.loads(
            i.replace("<tool_call>", "").replace("</tool_call>", "").strip()
        )
        characters = string.ascii_letters + string.digits
        call_id = "call_" + "".join(random.choices(characters, k=8))
        tool_name = tool_json["name"]
        tool_args = tool_json["arguments"]
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
    tool_json = json.loads(
        tool_txt.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    )
    characters = string.ascii_letters + string.digits
    call_id = "call_" + "".join(random.choices(characters, k=8))
    tool_name = tool_json["name"]
    tool_args = tool_json["arguments"]
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
