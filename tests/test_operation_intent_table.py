import json
import unittest

from agent.memory_guided_workflow.operation_intent_table import (
    OperationIntentTableGenerator,
    OperationVocabularyRetriever,
    render_markdown_table,
)
from agent.memory_guided_workflow.operation_vocabulary import build_operation_vocabulary


class FakeLLMClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def chat(self, messages):
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


class KeywordEmbeddingModel:
    keys = [
        "summarize",
        "summary",
        "rewrite",
        "paraphrase",
        "sentiment",
        "analyze",
    ]

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        if isinstance(texts, str):
            return self._encode_one(texts)
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text):
        lower_text = str(text).lower()
        return [float(lower_text.count(key)) for key in self.keys]


class TestOperationIntentTableGenerator(unittest.TestCase):
    def test_generates_ordered_intent_table(self) -> None:
        generator = OperationIntentTableGenerator(
            vocabulary=_sample_vocabulary(),
            llm_client=FakeLLMClient(
                {
                    "rows": [
                        {
                            "order": "1",
                            "intent": "summarize",
                            "operation_id": "summarize_text",
                            "candidate_tool": "Text Summarizer",
                            "description": "先对原始文章文本做摘要。",
                        },
                        {
                            "order": "2",
                            "intent": "rewrite",
                            "operation_id": "rewrite_text",
                            "candidate_tool": "Article Spinner",
                            "description": "对摘要进行改写。",
                        },
                        {
                            "order": "3",
                            "intent": "analyze",
                            "operation_id": "analyze_text_sentiment",
                            "candidate_tool": "Text Sentiment Analysis",
                            "description": "分析改写后摘要的情感。",
                        },
                    ]
                }
            ),
        )

        result = generator.generate("Summarize this article, paraphrase it, and analyze sentiment.")

        self.assertEqual([row.intent for row in result.rows], ["summarize", "rewrite", "analyze"])
        self.assertEqual([row.operation_id for row in result.rows], [
            "summarize_text",
            "rewrite_text",
            "analyze_text_sentiment",
        ])
        self.assertEqual(result.warnings, [])

        markdown = render_markdown_table(result)
        self.assertIn("| 顺序 | intent | operation_id | candidate tool | 说明 |", markdown)
        self.assertIn("| 2 | rewrite | rewrite_text | Article Spinner | 对摘要进行改写。 |", markdown)

    def test_replaces_invalid_intent_and_candidate_tool_from_vocabulary(self) -> None:
        generator = OperationIntentTableGenerator(
            vocabulary=_sample_vocabulary(),
            llm_client=FakeLLMClient(
                {
                    "rows": [
                        {
                            "order": "1",
                            "intent": "wrong",
                            "operation_id": "summarize_text",
                            "candidate_tool": "Made Up Tool",
                            "description": "摘要。",
                        }
                    ]
                }
            ),
        )

        result = generator.generate("Summarize this text.")

        self.assertEqual(result.rows[0].intent, "summarize")
        self.assertEqual(result.rows[0].candidate_tool, "Text Summarizer")
        self.assertEqual(len(result.warnings), 2)

    def test_skips_unknown_operation_id(self) -> None:
        generator = OperationIntentTableGenerator(
            vocabulary=_sample_vocabulary(),
            llm_client=FakeLLMClient(
                {
                    "rows": [
                        {
                            "order": "1",
                            "intent": "unknown",
                            "operation_id": "unknown_operation",
                            "candidate_tool": "Unknown Tool",
                            "description": "无效行。",
                        }
                    ]
                }
            ),
        )

        result = generator.generate("Do something unsupported.")

        self.assertEqual(result.rows, [])
        self.assertEqual(len(result.warnings), 1)

    def test_retrieves_operations_as_table(self) -> None:
        retriever = OperationVocabularyRetriever(
            vocabulary=_sample_vocabulary(),
            embedding_model=KeywordEmbeddingModel(),
        )
        generator = OperationIntentTableGenerator(
            vocabulary=_sample_vocabulary(),
            llm_client=FakeLLMClient({"rows": []}),
            retrieval_top_k=2,
            operation_retriever=retriever,
        )

        result = generator.retrieve_as_table("Please summarize this text.", top_k=2)

        self.assertEqual(result.rows[0].intent, "summarize")
        self.assertEqual(result.rows[0].operation_id, "summarize_text")
        self.assertEqual(result.rows[0].candidate_tool, "Text Summarizer")
        self.assertIn("retrieval mode ranks", result.warnings[0])


def _sample_vocabulary():
    return build_operation_vocabulary(
        {
            "nodes": [
                {
                    "id": "Text Summarizer",
                    "desc": "Summarizes a given text.",
                    "intent": "summarize",
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
                    "id": "Text Sentiment Analysis",
                    "desc": "Analyzes the sentiment of a given text.",
                    "intent": "analyze",
                    "input-type": ["text"],
                    "output-type": ["text"],
                },
            ]
        }
    )


if __name__ == "__main__":
    unittest.main()
