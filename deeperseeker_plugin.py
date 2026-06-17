import asyncio
import json
import os
import re
import sqlite3
import threading
import time
import uuid

from functions import create_new_chat, send_message
from litellm import ModelResponse, token_counter
from litellm.llms.custom_llm import CustomLLM
from litellm.types.utils import ChatCompletionDeltaToolCall
from litellm.types.utils import Delta as ChatCompletionStreamResponseDelta
from litellm.types.utils import Function as FunctionDelta
from plugin_helper import (
    build_prompt,
    extract_and_upload_files,
    parse_tool_call,
    parse_tool_call_streaming,
)


class DeeperSeekerProvider(CustomLLM):
    def __init__(self):
        super().__init__()
        if not os.path.exists("auth_token.txt"):
            print("Auth Token not added, run add_auth_token.txt to add it first.")
            print("For more see docs.")
            os._exit(0)
        self.auth_token = open("auth_token.txt").read().strip()
        self.sqlite_con = sqlite3.connect(
            "api_key_metadata.sqlite", check_same_thread=False
        )
        self.db_lock = threading.Lock()
        self.key_locks = {}
        self.key_locks_guard = threading.Lock()
        self.run_sqlite_init()

    def put_api_key_metadata(self, api_key, session_id, parent_message_id):
        with self.db_lock:
            cursor = self.sqlite_con.cursor()
            cursor.execute(
                """
                INSERT INTO api_key_metadata (api_key,session_id, parent_message_id)
                VALUES (?, ?, ?)
                ON CONFLICT(api_key,session_id)
                DO UPDATE SET
                    parent_message_id = excluded.parent_message_id;
            """,
                (api_key, session_id, parent_message_id),
            )
            self.sqlite_con.commit()

    def get_api_key_metadata(self, api_key):
        with self.db_lock:
            cursor = self.sqlite_con.cursor()
            cursor.execute(
                """SELECT
                session_id,
            parent_message_id
            FROM api_key_metadata
            WHERE api_key = ? ;""",
                (api_key,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            else:
                return row

    def run_sqlite_init(self):
        with self.db_lock:
            cursor = self.sqlite_con.cursor()
            with open("schema.sql", "r") as file:
                sql_script = file.read()
            cursor.executescript(sql_script)
            self.sqlite_con.commit()

    def completion(self, model, messages, **kwargs):
        litellm_params = kwargs.get("litellm_params")
        if litellm_params.get("metadata"):
            api_key = litellm_params.get("metadata").get("user_api_key_auth").api_key
        else:
            api_key = (
                litellm_params.get("litellm_metadata").get("user_api_key_auth").api_key
            )
        with self.key_locks_guard:
            if api_key not in self.key_locks:
                self.key_locks[api_key] = threading.Lock()
            key_lock = self.key_locks[api_key]
        key_lock.acquire()
        try:
            metadata = self.get_api_key_metadata(api_key)
            if not metadata:
                session_id = create_new_chat(self.auth_token)
                parent_message_id = 0
            else:
                session_id, parent_message_id = metadata
            file_ids = extract_and_upload_files(messages, self.auth_token)
            tools = kwargs.get("optional_params").get("tools", [])
            prompt = build_prompt(messages, tools, model, parent_message_id == 0)
            generator_message = send_message(
                session_id,
                self.auth_token,
                prompt,
                parent_message_id,
                kwargs.get("thinking", False),
                kwargs.get("search_needed", False),
                None if model == "instant" else model,
                file_ids,
            )
            response = ""
            tools_txt = []
            tool_coming = False
            current_tool_txt = ""
            for i in generator_message:
                if tool_coming:
                    current_tool_txt += i
                    is_possible_tool = (
                        "<tool_call>".startswith(current_tool_txt)
                        or "<tool_call>" in current_tool_txt
                    )
                    if not is_possible_tool and len(current_tool_txt) >= 15:
                        tool_coming = False
                        response += current_tool_txt
                        current_tool_txt = ""
                    elif "</tool_call>" in current_tool_txt:
                        raw, left_resp = current_tool_txt.split("</tool_call>", 1)
                        tools_txt.append(raw + "</tool_call>")
                        if "<" in left_resp:
                            before_angle, after_angle = left_resp.split("<", 1)
                            if before_angle:
                                response += before_angle
                            current_tool_txt = "<" + after_angle
                        elif left_resp:
                            response += left_resp
                            current_tool_txt = ""
                            tool_coming = False
                        else:
                            current_tool_txt = ""
                            tool_coming = False

                elif "<" in i:
                    before_start, after_start = i.split("<", 1)
                    if before_start:
                        response += before_start
                    tool_coming = True
                    current_tool_txt += "<" + after_start
                else:
                    response += i
            tools_called = parse_tool_call(tools_txt)
            for i in tools_called:
                if isinstance(i["function"]["arguments"], str):
                    i["function"]["arguments"] = json.dumps(
                        json.loads(i["function"]["arguments"])
                    )
                else:
                    i["function"]["arguments"] = json.dumps(i["function"]["arguments"])

            output_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                text=response,
            )
            input_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                messages=messages,
            )
            self.put_api_key_metadata(api_key, session_id, parent_message_id + 2)
        finally:
            key_lock.release()
        response = ModelResponse(
            id="chatcmpl-" + str(uuid.uuid4()),
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "tool_calls": tools_called if tools_called else None,
                        "role": "assistant",
                        "content": response,
                    },
                    "finish_reason": "tool_calls" if tools_called else "stop",
                }
            ],
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )
        return response

    async def acompletion(self, model, messages, **kwargs):

        litellm_params = kwargs.get("litellm_params")
        if litellm_params.get("metadata"):
            api_key = litellm_params.get("metadata").get("user_api_key_auth").api_key
        else:
            api_key = (
                litellm_params.get("litellm_metadata").get("user_api_key_auth").api_key
            )
        with self.key_locks_guard:
            if api_key not in self.key_locks:
                self.key_locks[api_key] = threading.Lock()
            key_lock = self.key_locks[api_key]
        await asyncio.to_thread(key_lock.acquire)
        try:
            metadata = await asyncio.to_thread(self.get_api_key_metadata, api_key)
            if not metadata:
                session_id = await asyncio.to_thread(create_new_chat, self.auth_token)
                parent_message_id = 0
            else:
                session_id, parent_message_id = metadata
            file_ids = await asyncio.to_thread(
                extract_and_upload_files, messages, self.auth_token
            )
            tools = kwargs.get("optional_params").get("tools", [])
            prompt = await asyncio.to_thread(
                build_prompt, messages, tools, model, parent_message_id == 0
            )
            generator_message = await asyncio.to_thread(
                send_message,
                session_id,
                self.auth_token,
                prompt,
                parent_message_id,
                kwargs.get("thinking", False),
                kwargs.get("search_needed", False),
                None if model == "instant" else model,
                file_ids,
            )
            END = object()
            response = ""
            tools_txt = []
            tool_coming = False
            current_tool_txt = ""
            while True:
                chunk = await asyncio.to_thread(next, generator_message, END)
                if chunk is END:
                    break
                if tool_coming:
                    current_tool_txt += chunk
                    is_possible_tool = (
                        "<tool_call>".startswith(current_tool_txt)
                        or "<tool_call>" in current_tool_txt
                    )
                    if not is_possible_tool and len(current_tool_txt) >= 15:
                        tool_coming = False
                        response += current_tool_txt
                        current_tool_txt = ""
                    elif "</tool_call>" in current_tool_txt:
                        raw, left_resp = current_tool_txt.split("</tool_call>", 1)
                        tools_txt.append(raw + "</tool_call>")
                        if "<" in left_resp:
                            before_angle, after_angle = left_resp.split("<", 1)
                            if before_angle:
                                response += before_angle
                            current_tool_txt = "<" + after_angle
                        elif left_resp:
                            response += left_resp
                            current_tool_txt = ""
                            tool_coming = False
                        else:
                            current_tool_txt = ""
                            tool_coming = False

                elif "<" in chunk:
                    before_start, after_start = chunk.split("<", 1)
                    if before_start:
                        response += before_start
                    tool_coming = True
                    current_tool_txt += "<" + after_start
                else:
                    response += chunk
            tools_called = await asyncio.to_thread(parse_tool_call, tools_txt)

            for i in tools_called:
                if isinstance(i["function"]["arguments"], str):
                    i["function"]["arguments"] = json.dumps(
                        json.loads(i["function"]["arguments"])
                    )
                else:
                    i["function"]["arguments"] = json.dumps(i["function"]["arguments"])

            output_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                text=response,
            )
            input_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                messages=messages,
            )
            await asyncio.to_thread(
                self.put_api_key_metadata, api_key, session_id, parent_message_id + 2
            )
        finally:
            key_lock.release()
        response = ModelResponse(
            id="chatcmpl-" + str(uuid.uuid4()),
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "tool_calls": tools_called if tools_called else None,
                        "role": "assistant",
                        "content": response,
                    },
                    "finish_reason": "tool_calls" if tools_called else "stop",
                }
            ],
            usage={
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
        )
        return response

    def streaming(self, model, messages, **kwargs):
        litellm_params = kwargs.get("litellm_params")
        if litellm_params.get("metadata"):
            api_key = litellm_params.get("metadata").get("user_api_key_auth").api_key
        else:
            api_key = (
                litellm_params.get("litellm_metadata").get("user_api_key_auth").api_key
            )
        with self.key_locks_guard:
            if api_key not in self.key_locks:
                self.key_locks[api_key] = threading.Lock()
            key_lock = self.key_locks[api_key]
        key_lock.acquire()
        try:
            metadata = self.get_api_key_metadata(api_key)
            if not metadata:
                session_id = create_new_chat(self.auth_token)
                parent_message_id = 0
            else:
                session_id, parent_message_id = metadata
            file_ids = extract_and_upload_files(messages, self.auth_token)

            tools = kwargs.get("optional_params").get("tools", [])
            prompt = build_prompt(messages, tools, model, parent_message_id == 0)
            generator_message = send_message(
                session_id,
                self.auth_token,
                prompt,
                parent_message_id,
                kwargs.get("thinking", False),
                kwargs.get("search_needed", False),
                None if model == "instant" else model,
                file_ids,
            )
            tool_coming = False
            response = ""
            current_tool_txt = ""
            tool_index = 0
            tools_called = False

            for chunk in generator_message:
                if tool_coming:
                    current_tool_txt += chunk

                    is_tool_tag = (
                        "<tool_call>".startswith(current_tool_txt)
                        or "<tool_call>" in current_tool_txt
                    )

                    if not is_tool_tag and len(current_tool_txt) >= 15:
                        yield {
                            "text": current_tool_txt,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += current_tool_txt
                        current_tool_txt = ""
                        tool_coming = False
                        continue

                    if "</tool_call>" in current_tool_txt:
                        raw, left_resp = current_tool_txt.split("</tool_call>", 1)
                        tool_json = parse_tool_call_streaming(
                            "<tool_call>" + raw + "</tool_call>"
                        )
                        tool_id = tool_json["id"]
                        name = tool_json["function"]["name"]
                        args_str = tool_json["function"]["arguments"]
                        if isinstance(args_str, str):
                            args_str = json.dumps(json.loads(args_str))
                        else:
                            args_str = json.dumps(args_str)
                        yield {
                            "text": "",
                            "tool_use": {
                                "id": tool_id,
                                "type": "function",
                                "index": tool_index,
                                "function": {"name": name, "arguments": args_str},
                            },
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        tool_index += 1
                        tools_called = True

                        if "<" in left_resp:
                            current_tool_txt = left_resp
                            tool_coming = True
                        elif left_resp:
                            yield {
                                "text": left_resp,
                                "tool_use": None,
                                "is_finished": False,
                                "finish_reason": None,
                                "usage": None,
                                "index": 0,
                            }
                            response += left_resp
                            current_tool_txt = ""
                            tool_coming = False
                        else:
                            current_tool_txt = ""
                            tool_coming = False

                else:
                    if "<" not in chunk:
                        yield {
                            "text": chunk,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += chunk
                        continue

                    before_tool, tool_useful = chunk.split("<", 1)
                    tool_coming = True
                    if before_tool:
                        yield {
                            "text": before_tool,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += before_tool
                    current_tool_txt = "<" + tool_useful

            if tool_coming and current_tool_txt:
                yield {
                    "text": current_tool_txt,
                    "tool_use": None,
                    "is_finished": False,
                    "finish_reason": None,
                    "usage": None,
                    "index": 0,
                }
                response += current_tool_txt

            output_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                text=response,
            )
            input_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                messages=messages,
            )

            self.put_api_key_metadata(api_key, session_id, parent_message_id + 2)
            yield {
                "text": "",
                "tool_use": None,
                "is_finished": True,
                "finish_reason": "tool_calls" if tools_called else "stop",
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                "index": 0,
            }
        finally:
            key_lock.release()

    async def astreaming(self, model, messages, **kwargs):

        litellm_params = kwargs.get("litellm_params")
        if litellm_params.get("metadata"):
            api_key = litellm_params.get("metadata").get("user_api_key_auth").api_key
        else:
            api_key = (
                litellm_params.get("litellm_metadata").get("user_api_key_auth").api_key
            )

        with self.key_locks_guard:
            if api_key not in self.key_locks:
                self.key_locks[api_key] = threading.Lock()
            key_lock = self.key_locks[api_key]
        await asyncio.to_thread(key_lock.acquire)
        try:
            metadata = await asyncio.to_thread(self.get_api_key_metadata, api_key)
            if not metadata:
                session_id = await asyncio.to_thread(create_new_chat, self.auth_token)
                parent_message_id = 0
            else:
                session_id, parent_message_id = metadata
            file_ids = await asyncio.to_thread(
                extract_and_upload_files, messages, self.auth_token
            )
            tools = kwargs.get("optional_params").get("tools", [])
            prompt = await asyncio.to_thread(
                build_prompt, messages, tools, model, parent_message_id == 0
            )
            generator_message = await asyncio.to_thread(
                send_message,
                session_id,
                self.auth_token,
                prompt,
                parent_message_id,
                kwargs.get("thinking", False),
                kwargs.get("search_needed", False),
                None if model == "instant" else model,
                file_ids,
            )
            tool_coming = False
            response = ""
            current_tool_txt = ""
            tool_index = 0
            tools_called = False
            END = object()

            while True:
                chunk = await asyncio.to_thread(next, generator_message, END)
                if chunk is END:
                    tool_coming = False
                    break
                if tool_coming:
                    current_tool_txt += chunk

                    is_tool_tag = (
                        "<tool_call>".startswith(current_tool_txt)
                        or "<tool_call>" in current_tool_txt
                    )

                    if not is_tool_tag and len(current_tool_txt) >= 15:
                        yield {
                            "text": current_tool_txt,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += current_tool_txt
                        current_tool_txt = ""
                        tool_coming = False
                        continue

                    if "</tool_call>" in current_tool_txt:
                        raw, left_resp = current_tool_txt.split("</tool_call>", 1)
                        tool_json = await asyncio.to_thread(
                            parse_tool_call_streaming,
                            "<tool_call>" + raw + "</tool_call>",
                        )
                        tool_id = tool_json["id"]
                        name = tool_json["function"]["name"]
                        args_str = tool_json["function"]["arguments"]
                        if isinstance(args_str, str):
                            args_str = json.dumps(json.loads(args_str))
                        else:
                            args_str = json.dumps(args_str)
                        yield {
                            "text": "",
                            "tool_use": {
                                "id": tool_id,
                                "type": "function",
                                "index": tool_index,
                                "function": {"name": name, "arguments": args_str},
                            },
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        tool_index += 1
                        tools_called = True

                        if "<" in left_resp:
                            current_tool_txt = left_resp
                            tool_coming = True
                        elif left_resp:
                            yield {
                                "text": left_resp,
                                "tool_use": None,
                                "is_finished": False,
                                "finish_reason": None,
                                "usage": None,
                                "index": 0,
                            }
                            response += left_resp
                            current_tool_txt = ""
                            tool_coming = False
                        else:
                            current_tool_txt = ""
                            tool_coming = False

                else:
                    if "<" not in chunk:
                        yield {
                            "text": chunk,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += chunk
                        continue

                    before_tool, tool_useful = chunk.split("<", 1)

                    tool_coming = True
                    if before_tool:
                        yield {
                            "text": before_tool,
                            "tool_use": None,
                            "is_finished": False,
                            "finish_reason": None,
                            "usage": None,
                            "index": 0,
                        }
                        response += before_tool
                    current_tool_txt = "<" + tool_useful

            if tool_coming and current_tool_txt:
                yield {
                    "text": current_tool_txt,
                    "tool_use": None,
                    "is_finished": False,
                    "finish_reason": None,
                    "usage": None,
                    "index": 0,
                }
                response += current_tool_txt

            output_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                text=response,
            )
            input_tokens = token_counter(
                model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
                messages=messages,
            )

            await asyncio.to_thread(
                self.put_api_key_metadata, api_key, session_id, parent_message_id + 2
            )
            yield {
                "text": "",
                "tool_use": None,
                "is_finished": True,
                "finish_reason": "tool_calls" if tools_called else "stop",
                "usage": {
                    "prompt_tokens": input_tokens,
                    "completion_tokens": output_tokens,
                    "total_tokens": input_tokens + output_tokens,
                },
                "index": 0,
            }
        finally:
            key_lock.release()


deeperseeker_instance = DeeperSeekerProvider()
