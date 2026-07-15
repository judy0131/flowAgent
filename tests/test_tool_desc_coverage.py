import json
import unittest

from agent.memory_guided_workflow.tool_desc_coverage import (
    ToolDescCoverageGenerator,
    render_markdown_table,
)


class FakeLLMClient:
    def __init__(self, payload):
        self.payload = payload
        self.messages = []

    def chat(self, messages):
        self.messages = messages
        return json.dumps(self.payload, ensure_ascii=False)


class TestToolDescCoverageGenerator(unittest.TestCase):
    def test_generates_covered_tool_descs_without_order(self) -> None:
        generator = ToolDescCoverageGenerator(
            tool_desc=_sample_tool_desc(),
            llm_client=FakeLLMClient(
                {
                    "covered_tools": [
                        {
                            "tool_id": "Text Summarizer",
                            "desc": "Summarizes a given text.",
                            "coverage_type": "direct",
                            "confidence": 0.98,
                            "request_evidence": "find the summarized version",
                            "coverage_reason": "The user explicitly asks for a summary of the article text.",
                            "input_output_fit": "The tool consumes text and returns text.",
                        },
                        {
                            "tool_id": "Text Sentiment Analysis",
                            "desc": "Analyzes the sentiment of a given text.",
                            "coverage_type": "direct",
                            "confidence": 0.96,
                            "request_evidence": "know the sentiment",
                            "coverage_reason": "The user explicitly asks to know sentiment.",
                            "input_output_fit": "The paraphrased summary is text, and the output is a sentiment text.",
                        },
                    ]
                }
            ),
        )

        result = generator.generate("Summarize an article and know the sentiment.")

        self.assertEqual(
            [row.tool_id for row in result.covered_tools],
            ["Text Summarizer", "Text Sentiment Analysis"],
        )
        self.assertEqual(result.warnings, [])

        markdown = render_markdown_table(result)
        self.assertIn("| tool_id | desc | 覆盖类型 | 置信度 | 请求证据 | 覆盖理由 | 输入输出匹配 |", markdown)
        self.assertIn("Text Sentiment Analysis", markdown)
        self.assertNotIn("顺序", markdown)

    def test_matches_by_desc_when_tool_id_is_missing(self) -> None:
        generator = ToolDescCoverageGenerator(
            tool_desc=_sample_tool_desc(),
            llm_client=FakeLLMClient(
                {
                    "covered_tools": [
                        {
                            "desc": "Rewrites a given text using different words.",
                            "coverage_type": "direct",
                            "confidence": 0.9,
                            "request_evidence": "paraphrase it",
                            "coverage_reason": "Paraphrasing is a rewrite request.",
                            "input_output_fit": "Text in, rewritten text out.",
                        }
                    ]
                }
            ),
        )

        result = generator.generate("Please paraphrase it.")

        self.assertEqual(result.covered_tools[0].tool_id, "Text Paraphraser")
        self.assertEqual(result.covered_tools[0].confidence, 0.9)

    def test_skips_unknown_tool(self) -> None:
        generator = ToolDescCoverageGenerator(
            tool_desc=_sample_tool_desc(),
            llm_client=FakeLLMClient(
                {
                    "covered_tools": [
                        {
                            "tool_id": "Unknown Tool",
                            "desc": "Does something unknown.",
                            "coverage_type": "direct",
                            "confidence": 1.0,
                        }
                    ]
                }
            ),
        )

        result = generator.generate("Do something unknown.")

        self.assertEqual(result.covered_tools, [])
        self.assertEqual(len(result.warnings), 1)


def _sample_tool_desc():
    return {
        "nodes": [
            {
                "id": "Text Summarizer",
                "desc": "Summarizes a given text.",
                "input-type": ["text"],
                "output-type": ["text"],
            },
            {
                "id": "Text Paraphraser",
                "desc": "Rewrites a given text using different words.",
                "input-type": ["text"],
                "output-type": ["text"],
            },
            {
                "id": "Text Sentiment Analysis",
                "desc": "Analyzes the sentiment of a given text.",
                "input-type": ["text"],
                "output-type": ["text"],
            },
        ]
    }


if __name__ == "__main__":
    unittest.main()
