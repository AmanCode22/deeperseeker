import asyncio
import json
import os
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
from plugin_helper import build_prompt, extract_and_upload_files, parse_tool_call


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
        api_key = kwargs.get("litellm_params").get("metadata").get("user_api_key")
        metadata = self.get_api_key_metadata(api_key)
        if not metadata:
            session_id = create_new_chat(self.auth_token)
            parent_message_id = 0
        else:
            session_id, parent_message_id = metadata
        file_ids = extract_and_upload_files(messages, self.auth_token)
        tools = kwargs.get("tools", [])
        prompt = build_prompt(messages, tools, parent_message_id == 0)
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
                if (
                    len(current_tool_txt) >= 15
                    and "<tool_call>" not in current_tool_txt
                ):
                    tool_coming = False
                    response += current_tool_txt
                    current_tool_txt = ""
                elif "</tool_call>" in current_tool_txt:
                    tools_txt.append(current_tool_txt)
                    current_tool_txt = ""
                    tool_coming = False
            elif "<" in i:
                tool_coming = True
                current_tool_txt += i
            else:
                response += i
        tools_called = parse_tool_call(tools_txt)
        if tools_called:
            for tool in tools_called:
                tool["function"]["arguments"] = json.dumps(
                    tool["function"]["arguments"]
                )
        output_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            text=response,
        )
        input_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            messages=messages,
        )
        self.put_api_key_metadata(api_key, session_id, parent_message_id + 2)
        response = ModelResponse(
            id="chatcmpl-" + str(uuid.uuid4()),
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "tool_calls": tools_called,
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
        api_key = kwargs.get("litellm_params").get("metadata").get("user_api_key")
        metadata = await asyncio.to_thread(self.get_api_key_metadata, api_key)
        if not metadata:
            session_id = await asyncio.to_thread(create_new_chat, self.auth_token)
            parent_message_id = 0
        else:
            session_id, parent_message_id = metadata
        file_ids = await asyncio.to_thread(
            extract_and_upload_files, messages, self.auth_token
        )
        tools = kwargs.get("tools", [])
        prompt = await asyncio.to_thread(
            build_prompt, messages, tools, parent_message_id == 0
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
                if (
                    len(current_tool_txt) >= 15
                    and "<tool_call>" not in current_tool_txt
                ):
                    tool_coming = False
                    response += current_tool_txt
                    current_tool_txt = ""
                elif "</tool_call>" in current_tool_txt:
                    tools_txt.append(current_tool_txt)
                    current_tool_txt = ""
                    tool_coming = False
            elif "<" in chunk:
                tool_coming = True
                current_tool_txt += chunk
            else:
                response += chunk
        tools_called = await asyncio.to_thread(parse_tool_call, tools_txt)
        if tools_called:
            for tool in tools_called:
                tool["function"]["arguments"] = json.dumps(
                    tool["function"]["arguments"]
                )
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
        response = ModelResponse(
            id="chatcmpl-" + str(uuid.uuid4()),
            object="chat.completion",
            created=int(time.time()),
            model=model,
            choices=[
                {
                    "index": 0,
                    "message": {
                        "tool_calls": tools_called,
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
        api_key = kwargs.get("litellm_params").get("metadata").get("user_api_key")
        metadata = self.get_api_key_metadata(api_key)
        if not metadata:
            session_id = create_new_chat(self.auth_token)
            parent_message_id = 0
        else:
            session_id, parent_message_id = metadata
        file_ids = extract_and_upload_files(messages, self.auth_token)
        tools = kwargs.get("tools", [])
        prompt = build_prompt(messages, tools, parent_message_id == 0)
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
        tools_txt = []
        tool_coming = False
        response = ""
        completion_id = f"chatcmpl-{uuid.uuid4()}"
        created_time = int(time.time())
        current_tool_txt = ""

        for chunk in generator_message:
            if tool_coming:
                if (
                    not "<tool_call>" in current_tool_txt
                    and len(current_tool_txt) >= 15
                ):
                    yield {
                        "finish_reason": None,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "is_finished": False,
                        "usage": None,
                        "text": current_tool_txt,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": current_tool_txt,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    response += current_tool_txt
                    current_tool_txt = ""
                    tool_coming = False
                elif "</tool_call>" in current_tool_txt:
                    tool_coming = False
                    tools_txt.append(current_tool_txt)
                    current_tool_txt = ""
                else:
                    current_tool_txt += chunk
            else:
                if "<" not in chunk:
                    yield {
                        "finish_reason": None,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "is_finished": False,
                        "usage": None,
                        "text": chunk,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": chunk,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    response += chunk
                    continue
                tool_coming = True
                before_tool, tool_useful = chunk.split("<")
                if before_tool != "":
                    response += before_tool
                    yield {
                        "finish_reason": None,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "is_finished": False,
                        "usage": None,
                        "text": before_tool,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": before_tool,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                current_tool_txt += tool_useful
                tool_coming = True
        if tool_coming:
            yield {
                "finish_reason": None,
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created_time,
                "model": model,
                "is_finished": False,
                "usage": None,
                "text": current_tool_txt,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": current_tool_txt,
                        },
                        "finish_reason": None,
                    }
                ],
            }
        tools_called = parse_tool_call(tools_txt)
        output_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            text=response,
        )
        input_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            messages=messages,
        )

        if tools_called:
            for tool_index, tool in enumerate(tools_called):
                tool_id = tool["id"]
                name = tool["function"]["name"]
                args = tool["function"]["arguments"]

                args_str = json.dumps(args) if isinstance(args, dict) else str(args)
                arg_chunks = [args_str[k : k + 8] for k in range(0, len(args_str), 8)]

                for chunk_id, arg_piece in enumerate(arg_chunks):
                    if chunk_id == 0:
                        delta_tool = {
                            "index": tool_index,
                            "id": tool_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arg_piece},
                        }
                    else:
                        delta_tool = {
                            "index": tool_index,
                            "function": {"arguments": arg_piece},
                        }

                    yield {
                        "usage": None,
                        "finish_reason": None,
                        "is_finished": False,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "text": "",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [delta_tool],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
        self.put_api_key_metadata(api_key, session_id, parent_message_id + 2)
        yield {
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "finish_reason": "tool_calls" if tools_called else "stop",
            "is_finished": True,
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model,
            "text": "",
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": None},
                    "finish_reason": "tool_calls" if tools_called else "stop",
                }
            ],
        }

    async def astreaming(self, model, messages, **kwargs):
        api_key = kwargs.get("litellm_params").get("metadata").get("user_api_key")
        metadata = await asyncio.to_thread(self.get_api_key_metadata, api_key)
        if not metadata:
            session_id = await asyncio.to_thread(create_new_chat, self.auth_token)
            parent_message_id = 0
        else:
            session_id, parent_message_id = metadata
        file_ids = await asyncio.to_thread(
            extract_and_upload_files, messages, self.auth_token
        )
        tools = kwargs.get("tools", [])
        prompt = await asyncio.to_thread(
            build_prompt, messages, tools, parent_message_id == 0
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
        response = ""
        current_tool_txt = ""
        completion_id = f"chatcmpl-{uuid.uuid4()}"
        created_time = int(time.time())
        tool_coming = False
        tools_txt = []
        END = object()
        while True:
            chunk = await asyncio.to_thread(next, generator_message, END)
            if chunk is END:
                break
            if tool_coming:
                current_tool_txt += chunk
                if (
                    len(current_tool_txt) >= 15
                    and "<tool_call>" not in current_tool_txt
                ):
                    tool_coming = False
                    response += current_tool_txt
                    yield {
                        "finish_reason": None,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "is_finished": False,
                        "usage": None,
                        "text": current_tool_txt,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "content": current_tool_txt,
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                    current_tool_txt = ""
                elif "</tool_call>" in current_tool_txt:
                    tools_txt.append(current_tool_txt)
                    current_tool_txt = ""
                    tool_coming = False
            elif "<" in chunk:
                tool_coming = True
                current_tool_txt += chunk
            else:
                response += chunk
                yield {
                    "finish_reason": None,
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created_time,
                    "model": model,
                    "is_finished": False,
                    "usage": None,
                    "text": chunk,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": chunk},
                            "finish_reason": None,
                        }
                    ],
                }

        tools_called = parse_tool_call(tools_txt)
        output_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            text=response,
        )
        input_tokens = token_counter(
            model="deepseek-v4-pro" if model == "expert" else "deepseek-v4-flash",
            messages=messages,
        )

        if tools_called:
            for tool_index, j in enumerate(tools_called):
                tool_id = j["id"]
                name = j["function"]["name"]
                args = j["function"]["arguments"]
                args_str = json.dumps(args)
                arg_chunks = [args_str[k : k + 8] for k in range(0, len(args_str), 8)]
                for chunk_id, arg_piece in enumerate(arg_chunks):
                    if chunk_id == 0:
                        delta_tool = ChatCompletionDeltaToolCall(
                            index=tool_index,
                            id=tool_id,
                            type="function",
                            function=FunctionDelta(name=name, arguments=arg_piece),
                        )
                    else:
                        delta_tool = ChatCompletionDeltaToolCall(
                            index=tool_index,
                            function=FunctionDelta(arguments=arg_piece),
                        )
                    yield {
                        "finish_reason": None,
                        "is_finished": False,
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created_time,
                        "model": model,
                        "usage": None,
                        "text": "",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "role": "assistant",
                                    "tool_calls": [
                                        delta_tool.model_dump()
                                        if hasattr(delta_tool, "model_dump")
                                        else delta_tool
                                    ],
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
        await asyncio.to_thread(
            self.put_api_key_metadata, api_key, session_id, parent_message_id + 2
        )
        yield {
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
            "finish_reason": "tool_calls" if tools_called else "stop",
            "is_finished": True,
            "id": completion_id,
            "text": "",
            "object": "chat.completion.chunk",
            "created": created_time,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": None},
                    "finish_reason": "tool_calls" if tools_called else "stop",
                }
            ],
        }


deeperseeker_instance = DeeperSeekerProvider()
