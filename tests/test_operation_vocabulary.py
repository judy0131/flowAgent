import unittest

from agent.memory_guided_workflow.operation_vocabulary import (
    build_operation_vocabulary,
    render_ascii_tree,
    render_mermaid_tree,
)


class TestOperationVocabulary(unittest.TestCase):
    def test_builds_intent_operation_tool_tree(self) -> None:
        vocabulary = build_operation_vocabulary(
            {
                "nodes": [
                    {
                        "id": "Image Downloader",
                        "desc": "Downloads an image from a given URL.",
                        "intent": "download",
                        "input-type": ["url"],
                        "output-type": ["Image"],
                    },
                    {
                        "id": "Image-to-Text",
                        "desc": "Extracts text from an input image using OCR.",
                        "intent": "transcribe",
                        "input-type": ["image"],
                        "output-type": ["text"],
                    },
                    {
                        "id": "Text Paraphraser",
                        "desc": "Rewrites a given text using different words.",
                        "intent": "rewrite",
                        "input-type": ["text"],
                        "output-type": ["text"],
                    },
                    {
                        "id": "Article Spinner",
                        "desc": "Rewrites a given article using synonyms.",
                        "intent": "rewrite",
                        "input-type": ["text"],
                        "output-type": ["text"],
                    },
                    {
                        "id": "Image-to-Video",
                        "desc": "Creates a slideshow video using two input images.",
                        "intent": "create",
                        "input-type": ["image", "image"],
                        "output-type": ["video"],
                    },
                    {
                        "id": "Audio Noise Reduction",
                        "desc": "Reduces background noise from a given audio file.",
                        "intent": "reduce",
                        "input-type": ["audio"],
                        "output-type": ["audio"],
                    },
                ]
            }
        )

        self.assertEqual(vocabulary["metadata"]["intent_count"], 5)
        self.assertEqual(vocabulary["metadata"]["operation_count"], 5)
        self.assertEqual(vocabulary["metadata"]["tool_count"], 6)

        operations = _operation_index(vocabulary)
        self.assertIn("download_image", operations)
        self.assertIn("transcribe_text", operations)
        self.assertIn("rewrite_text", operations)
        self.assertIn("create_video", operations)
        self.assertIn("reduce_audio_noise", operations)
        self.assertEqual(
            [tool["tool_id"] for tool in operations["rewrite_text"]["candidate_tools"]],
            ["Article Spinner", "Text Paraphraser"],
        )

    def test_renders_tree_and_mermaid(self) -> None:
        vocabulary = build_operation_vocabulary(
            {
                "nodes": [
                    {
                        "id": "Text Translator",
                        "desc": "Translates a given text.",
                        "intent": "translate",
                        "input-type": ["text"],
                        "output-type": ["text"],
                    }
                ]
            }
        )

        ascii_tree = render_ascii_tree(vocabulary)
        mermaid = render_mermaid_tree(vocabulary)

        self.assertIn("intent: translate", ascii_tree)
        self.assertIn("operation: translate_text", ascii_tree)
        self.assertIn("tool: Text Translator", ascii_tree)
        self.assertIn("graph TD", mermaid)
        self.assertIn("Operation Vocabulary", mermaid)
        self.assertIn("tool: Text Translator", mermaid)


def _operation_index(vocabulary):
    return {
        operation["operation_id"]: operation
        for intent in vocabulary["intents"]
        for operation in intent["operations"]
    }


if __name__ == "__main__":
    unittest.main()
