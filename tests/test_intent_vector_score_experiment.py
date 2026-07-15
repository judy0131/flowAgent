import json
import tempfile
import unittest
from pathlib import Path

from agent.memory_guided_workflow.experiments.intent_vector_score_experiment import (
    IntentVectorScorer,
    load_intents,
    render_markdown_table,
)


class KeywordEmbeddingModel:
    keys = ["summary", "summarize", "sentiment", "paraphrase"]

    def encode(self, texts, convert_to_numpy=True, show_progress_bar=False):
        if isinstance(texts, str):
            return self._encode_one(texts)
        return [self._encode_one(text) for text in texts]

    def _encode_one(self, text):
        lower_text = str(text).lower()
        return [float(lower_text.count(key)) for key in self.keys]


class TestIntentVectorScoreExperiment(unittest.TestCase):
    def test_scores_intents_without_tool_or_desc_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tool_desc.json"
            path.write_text(json.dumps(_sample_tool_desc()), encoding="utf-8")

            scorer = IntentVectorScorer(
                tool_desc_path=path,
                embedding_model=KeywordEmbeddingModel(),
            )
            scores = scorer.score("Please summarize this article.", top_k=2)

        self.assertEqual(scores[0].intent, "SummarizeTextToShorterVersion")
        markdown = render_markdown_table(scores)
        self.assertIn("| rank | score | intent |", markdown)
        self.assertNotIn("tool_id", markdown)
        self.assertNotIn("desc", markdown)

    def test_loads_unique_intents(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "tool_desc.json"
            path.write_text(json.dumps(_sample_tool_desc()), encoding="utf-8")

            intents = load_intents(path)

        self.assertEqual(
            intents,
            [
                "SummarizeTextToShorterVersion",
                "AnalysisSentimentOfText",
                "ParaphraseTextUsingDifferentWords",
            ],
        )


def _sample_tool_desc():
    return {
        "nodes": [
            {
                "id": "Text Summarizer",
                "desc": "Summarizes a given text.",
                "intent": "SummarizeTextToShorterVersion",
            },
            {
                "id": "Text Sentiment Analysis",
                "desc": "Analyzes the sentiment of a given text.",
                "intent": "AnalysisSentimentOfText",
            },
            {
                "id": "Text Paraphraser",
                "desc": "Rewrites a given text using different words.",
                "intent": "ParaphraseTextUsingDifferentWords",
            },
        ]
    }


if __name__ == "__main__":
    unittest.main()
