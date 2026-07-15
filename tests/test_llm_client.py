import unittest
from types import SimpleNamespace

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


class _RecordingCompletions:
    def __init__(self) -> None:
        self.request_kwargs = None

    def create(self, **kwargs):
        self.request_kwargs = kwargs
        message = SimpleNamespace(content="ok")
        choice = SimpleNamespace(message=message)
        return SimpleNamespace(choices=[choice])


class _RecordingChatClient(OpenAICompatibleLLMClient):
    def __init__(self, llm_config):
        super().__init__(llm_config=llm_config)
        self.completions = _RecordingCompletions()
        self.recording_client = SimpleNamespace(
            chat=SimpleNamespace(completions=self.completions)
        )

    def _get_chat_client(self, **kwargs):
        return self.recording_client


class TestOpenAICompatibleLLMClient(unittest.TestCase):
    def test_resolve_config_is_cached_per_client(self) -> None:
        client = _CountingConfigClient()

        first = client.resolve_config()
        second = client.resolve_config()

        self.assertEqual(first, second)
        self.assertEqual(client.load_count, 1)

    def test_chat_passes_extra_body_to_openai_client(self) -> None:
        client = _RecordingChatClient(
            {
                "model_name": "qwen3-14b",
                "api_key": "sk-test-key",
                "extra_body": {"enable_thinking": False},
            }
        )

        content = client.chat([{"role": "user", "content": "hi"}])

        self.assertEqual(content, "ok")
        self.assertEqual(
            client.completions.request_kwargs["extra_body"],
            {"enable_thinking": False},
        )

    def test_resolve_extra_body_rejects_non_object_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "extra_body"):
            OpenAICompatibleLLMClient.resolve_extra_body({"extra_body": []})


if __name__ == "__main__":
    unittest.main()
