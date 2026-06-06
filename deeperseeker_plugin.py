import os
import time
import uuid

from functions import create_new_chat, send_message
from litellm import ModelResponse
from litellm.llms.custom_llm import CustomLLM
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


deeperseeker_instance = DeeperSeekerProvider()
