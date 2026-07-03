import asyncio
import base64
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
            else:
                for j in i["content"]:
                    if j["type"] == "text":
                        return j["text"]


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
    tools_result_extract = await extract_tool_results(messages)
    if tools_result_extract:
        final_prompt += f"""[TOOL RESULTS]\n{tools_result_extract}\n\n"""
    final_prompt += f"""[USER]\n{await extract_user_msg(messages)}\n\n"""
    if tools_extract:
        final_prompt += """If you need to call a tool, emit it like this — anywhere in your response:
        <tool_call>{"name": "tool_name", "arguments": {"param": "value"}}</tool_call> .While calling tools if subagents available then if optional then try to skip it."""
    final_prompt += f"Just for reminding if you need model name then your model name is {model}. By the way your provider name is deeperseeker."
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
