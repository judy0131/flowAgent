import unittest
import sys
from types import SimpleNamespace

sys.modules.setdefault("aiohttp", SimpleNamespace(ClientSession=object))
from taskbench.inference import apply_non_streaming_model_options, loads_model_json_content


class TestTaskbenchInference(unittest.TestCase):
    def test_qwen3_non_streaming_disables_thinking(self) -> None:
        payload = apply_non_streaming_model_options(
            {
                "model": "qwen3-14b",
                "stream": False,
            }
        )

        self.assertIs(payload["enable_thinking"], False)

    def test_non_qwen3_removes_thinking_option(self) -> None:
        payload = apply_non_streaming_model_options(
            {
                "model": "gpt-4o-mini",
                "stream": False,
                "enable_thinking": False,
            }
        )

        self.assertNotIn("enable_thinking", payload)

    def test_loads_model_json_content_repairs_escaped_apostrophe(self) -> None:
        content = (
            r'{"task_steps":["Step 1: Call send_sms tool with phone_number: '
            r"'+1234567890' and content: 'Hey, I want to recommend you listen "
            r'to our favorite track called \"Stayin\' Alive\" by Bee Gees. It\'s a classic!"],'
            r'"task_nodes":[{"task":"play_music_by_title","arguments":[{"name":"title","value":"Stayin\' Alive"}]}],'
            r'"task_links":[]}'
        )

        payload = loads_model_json_content(content)

        self.assertEqual(payload["task_nodes"][0]["arguments"][0]["value"], "Stayin' Alive")
        self.assertIn('"Stayin\' Alive"', payload["task_steps"][0])


if __name__ == "__main__":
    unittest.main()
