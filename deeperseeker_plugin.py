import asyncio
import json
import os
import time
import uuid

from functions import create_new_chat, send_message
from litellm import (
    ModelResponse,
    ModelResponseStream,
)
from litellm.llms.custom_llm import CustomLLM
from litellm.types.utils import ChatCompletionDeltaToolCall, StreamingChoices
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

    def completion(self, model, messages, kwargs):
        metadata = kwargs.get("metadata", {})
        if (
            metadata == {}
            or metadata.get("session_id") is None
            or metadata.get("parent_message_id") is None
        ):
            session_id = create_new_chat(self.auth_token)
            parent_message_id = 0
        else:
            session_id = metadata["session_id"]
            parent_message_id = metadata["parent_message_id"]
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
        for i in generator_message:
            response += i
        parsed, tools_called = parse_tool_call(response)
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
                        "content": parsed,
                    },
                    "finish_reason": "tool_calls" if tools_called else "stop",
                    "metadata": {
                        "session_id": session_id,
                        "parent_message_id": parent_message_id + 2,
                    },
                }
            ],
        )
        return response

    async def acompletion(self, model, messages, kwargs):
        metadata = kwargs.get("metadata", {})
        if (
            metadata == {}
            or metadata.get("session_id") is None
            or metadata.get("parent_message_id") is None
        ):
            session_id = await asyncio.to_thread(create_new_chat, self.auth_token)
            parent_message_id = 0
        else:
            session_id = metadata["session_id"]
            parent_message_id = metadata["parent_message_id"]
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
        while True:
            chunk = await asyncio.to_thread(next, generator_message, END)
            if chunk is END:
                break
            response += chunk
        parsed, tools_called = await asyncio.to_thread(parse_tool_call, response)

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
                        "content": parsed,
                    },
                    "finish_reason": "tool_calls" if tools_called else "stop",
                    "metadata": {
                        "session_id": session_id,
                        "parent_message_id": parent_message_id + 2,
                    },
                }
            ],
        )
        return response

    def streaming(self, model, messages, kwargs):
        metadata = kwargs.get("metadata", {})
        if (
            metadata == {}
            or metadata.get("session_id") is None
            or metadata.get("parent_message_id") is None
        ):
            session_id = create_new_chat(self.auth_token)
            parent_message_id = 0
        else:
            session_id = metadata["session_id"]
            parent_message_id = metadata["parent_message_id"]
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

        for i in generator_message:
            response += i
        parsed, tools_called = parse_tool_call(response)
        completion_id = f"chatcmpl-{uuid.uuid4()}"
        created_time = int(time.time())
        parsed_stream = [parsed[i : i + 12] for i in range(0, len(parsed), 12)]

        for i in parsed_stream:
            yield ModelResponseStream(
                id=completion_id,
                object="chat.completion.chunk",
                created=created_time,
                model=model,
                choices=[
                    StreamingChoices(
                        index=0,
                        delta=ChatCompletionStreamResponseDelta(
                            role="assistant", content=i
                        ),
                        finish_reason=None,
                    )
                ],
            )

        if tools_called:
            for tool_index, i in enumerate(tools_called):
                tool_id = i["id"]
                name = i["function"]["name"]
                args = i["function"]["arguments"]
                args = json.dumps(args)
                arg_chunks = [args[j : j + 8] for j in range(0, len(args), 8)]
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
                    yield ModelResponseStream(
                        id=completion_id,
                        object="chat.completion.chunk",
                        created=created_time,
                        model=model,
                        choices=[
                            StreamingChoices(
                                index=0,
                                delta=ChatCompletionStreamResponseDelta(
                                    role="assistant", tool_calls=[delta_tool]
                                ),
                                finish_reason=None,
                            )
                        ],
                    )

        yield ModelResponseStream(
            id=completion_id,
            object="chat.completion.chunk",
            created=created_time,
            model=model,
            choices=[
                StreamingChoices(
                    index=0,
                    delta=ChatCompletionStreamResponseDelta(content=None),
                    finish_reason="tool_calls" if tools_called else "stop",
                )
            ],
            metadata={
                "session_id": session_id,
                "parent_message_id": parent_message_id + 2,
            },
        )


deeperseeker_instance = DeeperSeekerProvider()
