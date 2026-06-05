import os

from litellm.llms.custom_llm import CustomLLM


class DeeperSeekerProvider(CustomLLM):
    def __init__(self):
        super().__init__()
        if not os.path.exists("auth_token.txt"):
            print("Auth Token not added, run add_auth_token.txt to add it first.")
            print("For more see docs.")
            os._exit(0)
        self.auth_token = open("auth_token.txt").read().strip()

    def completion(self, model, messages, kwargs):
        pass


deeperseeker_instance = DeeperSeekerProvider()
