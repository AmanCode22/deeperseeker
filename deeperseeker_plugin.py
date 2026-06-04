from litellm.llms.custom_llm import CustomLLM


class DeeperSeekerProvider(CustomLLM):
    def completion(self, model, messages, **kwargs):
        pass


deeperseeker_instance = DeeperSeekerProvider()
