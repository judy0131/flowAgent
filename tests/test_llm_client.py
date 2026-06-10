import unittest

from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient


class _CountingConfigClient(OpenAICompatibleLLMClient):
    def __init__(self):
        super().__init__()
        self.load_count = 0

    def load_config_payload(self):
        self.load_count += 1
        return {
            "model_name": "test-model",
            "api_key": "sk-test-key",
        }


class TestOpenAICompatibleLLMClient(unittest.TestCase):
    def test_resolve_config_is_cached_per_client(self) -> None:
        client = _CountingConfigClient()

        first = client.resolve_config()
        second = client.resolve_config()

        self.assertEqual(first, second)
        self.assertEqual(client.load_count, 1)


if __name__ == "__main__":
    unittest.main()
