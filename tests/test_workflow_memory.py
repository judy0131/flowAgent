import unittest
from pathlib import Path

from agent.pipeline_orchestrator.retrieval import (
    WorkflowMemoryRetriever,
    format_workflow_memory_prompt_block,
    score_workflow_with_retrieval_context,
)
from agent.pipeline_orchestrator.workflow_memory import (
    WorkflowMemoryIndex,
    assign_case_id_to_fold,
    load_case_id_file,
    select_taskbench_records,
)
from agent.pipeline_orchestrator_agent import PipelineOrchestratorAgent, SkillRegistry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = PROJECT_ROOT / "skills" / "operators"


class TestWorkflowMemoryIndex(unittest.TestCase):
    def test_build_from_taskbench_records_extracts_aggregate_priors(self) -> None:
        records = [
            {
                "id": "1",
                "type": "chain",
                "instruction": "Simplify the article, summarize it, then search for related topics.",
                "tool_nodes": """
                [
                  {"task": "Text Simplifier", "arguments": ["article"]},
                  {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                  {"task": "Text Search", "arguments": ["<node-1>"]}
                ]
                """,
                "tool_links": """
                [
                  {"source": "Text Simplifier", "target": "Text Summarizer"},
                  {"source": "Text Summarizer", "target": "Text Search"}
                ]
                """,
            },
            {
                "id": "2",
                "type": "chain",
                "instruction": "Transcribe the audio, add effects, and render a waveform image.",
                "tool_nodes": """
                [
                  {"task": "Audio-to-Text", "arguments": ["example.wav"]},
                  {"task": "Audio Effects", "arguments": ["example.wav", "<node-0>"]},
                  {"task": "Audio-to-Image", "arguments": ["<node-1>"]}
                ]
                """,
                "tool_links": """
                [
                  {"source": "Audio-to-Text", "target": "Audio Effects"},
                  {"source": "Audio Effects", "target": "Audio-to-Image"}
                ]
                """,
            },
        ]

        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")

        self.assertEqual(memory.transition_counts[("Text Simplifier", "Text Summarizer")], 1)
        self.assertEqual(memory.transition_counts[("Audio Effects", "Audio-to-Image")], 1)
        motif_ids = {motif.motif_id for motif in memory.motifs}
        self.assertIn("Text Simplifier -> Text Summarizer", motif_ids)
        self.assertIn("Audio-to-Text -> Audio Effects -> Audio-to-Image", motif_ids)
        waveform_motif = next(motif for motif in memory.motifs if motif.motif_id == "Audio-to-Text -> Audio Effects -> Audio-to-Image")
        self.assertEqual(
            waveform_motif.links,
            (("Audio-to-Text", "Audio Effects"), ("Audio Effects", "Audio-to-Image")),
        )
        self.assertIn("transcribe", waveform_motif.action_tags)
        self.assertIn("waveform", waveform_motif.action_tags)

    def test_graph_path_motifs_follow_actual_links_not_raw_node_order(self) -> None:
        records = [
            {
                "id": "dag-1",
                "type": "dag",
                "instruction": "Simplify the article, summarize it, generate topics, then search using the topics.",
                "tool_nodes": [
                    {"task": "Text Simplifier", "arguments": ["article"]},
                    {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                    {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
                    {"task": "Topic Generator", "arguments": ["<node-1>"]},
                    {"task": "Text Search", "arguments": ["<node-3>"]},
                ],
                "tool_links": [
                    {"source": "Text Simplifier", "target": "Text Summarizer"},
                    {"source": "Text Summarizer", "target": "Keyword Extractor"},
                    {"source": "Text Summarizer", "target": "Topic Generator"},
                    {"source": "Topic Generator", "target": "Text Search"},
                ],
            }
        ]

        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")
        motif_ids = {motif.motif_id for motif in memory.motifs}

        self.assertIn("Text Simplifier -> Text Summarizer -> Topic Generator -> Text Search", motif_ids)
        self.assertNotIn("Text Simplifier -> Text Summarizer -> Keyword Extractor -> Topic Generator", motif_ids)
        self.assertEqual(
            memory.transition_counts[("Text Simplifier", "Text Summarizer")],
            1,
        )
        self.assertEqual(
            memory.transition_counts[("Text Summarizer", "Keyword Extractor")],
            1,
        )
        self.assertEqual(
            memory.transition_counts[("Text Summarizer", "Topic Generator")],
            1,
        )
        self.assertEqual(
            memory.transition_counts[("Topic Generator", "Text Search")],
            1,
        )

    def test_reference_edges_support_one_based_node_placeholders(self) -> None:
        records = [
            {
                "id": "one-based-1",
                "type": "chain",
                "instruction": "Simplify the article and summarize it.",
                "tool_nodes": [
                    {"task": "Text Simplifier", "arguments": ["article"]},
                    {"task": "Text Summarizer", "arguments": ["<node-1>"]},
                ],
                "tool_links": [
                    {"source": "Text Simplifier", "target": "Text Summarizer"},
                ],
            }
        ]

        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")

        self.assertEqual(
            memory.transition_counts[("Text Simplifier", "Text Summarizer")],
            1,
        )

    def test_memory_serialization_drops_case_level_fields(self) -> None:
        records = [
            {
                "id": "1",
                "type": "chain",
                "instruction": "Simplify the article and summarize it.",
                "tool_nodes": [
                    {"task": "Text Simplifier", "arguments": ["article"]},
                    {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                ],
                "tool_links": [
                    {"source": "Text Simplifier", "target": "Text Summarizer"},
                ],
            }
        ]

        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")
        payload = memory.to_dict()

        self.assertNotIn("cases", payload)
        self.assertIn("motifs", payload)
        self.assertNotIn("example_case_ids", payload["motifs"][0])

    def test_select_taskbench_records_supports_include_exclude_and_fold(self) -> None:
        records = [
            {"id": "a", "instruction": "one", "tool_nodes": [{"task": "Text Simplifier"}], "tool_links": []},
            {"id": "b", "instruction": "two", "tool_nodes": [{"task": "Text Search"}], "tool_links": []},
            {"id": "c", "instruction": "three", "tool_nodes": [{"task": "Topic Generator"}], "tool_links": []},
        ]

        selected = select_taskbench_records(records, include_ids={"a", "c"}, exclude_ids={"c"})
        self.assertEqual([record["id"] for record in selected], ["a"])

        fold_for_a = assign_case_id_to_fold("a", 3)
        included = select_taskbench_records(records, num_folds=3, fold_index=fold_for_a, fold_mode="include")
        self.assertEqual([record["id"] for record in included], ["a"])

        excluded = select_taskbench_records(records, num_folds=3, fold_index=fold_for_a, fold_mode="exclude")
        self.assertEqual({record["id"] for record in excluded}, {"b", "c"})

    def test_load_case_id_file_ignores_comments_and_blank_lines(self) -> None:
        path = PROJECT_ROOT / "tests" / "_tmp_case_ids.txt"
        try:
            path.write_text("\n# comment\n111\n\n222\n", encoding="utf-8")
            self.assertEqual(load_case_id_file(path), {"111", "222"})
        finally:
            if path.exists():
                path.unlink()


class TestWorkflowMemoryRetrieval(unittest.TestCase):
    def setUp(self) -> None:
        records = [
            {
                "id": "1",
                "type": "chain",
                "instruction": "Simplify the article, summarize it, then search for related topics.",
                "tool_nodes": [
                    {"task": "Text Simplifier", "arguments": ["article"]},
                    {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                    {"task": "Text Search", "arguments": ["<node-1>"]},
                ],
                "tool_links": [
                    {"source": "Text Simplifier", "target": "Text Summarizer"},
                    {"source": "Text Summarizer", "target": "Text Search"},
                ],
            },
            {
                "id": "2",
                "type": "chain",
                "instruction": "Transcribe the audio, add effects, and render a waveform image.",
                "tool_nodes": [
                    {"task": "Audio-to-Text", "arguments": ["example.wav"]},
                    {"task": "Audio Effects", "arguments": ["example.wav", "<node-0>"]},
                    {"task": "Audio-to-Image", "arguments": ["<node-1>"]},
                ],
                "tool_links": [
                    {"source": "Audio-to-Text", "target": "Audio Effects"},
                    {"source": "Audio Effects", "target": "Audio-to-Image"},
                ],
            },
        ]
        self.memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")
        self.retriever = WorkflowMemoryRetriever(self.memory)

    def test_retriever_returns_relevant_motifs_and_transitions(self) -> None:
        context = self.retriever.retrieve(
            "Please simplify this article and search for related topics on the web.",
            detected_actions=["simplify", "retrieval", "topic"],
        )

        transition_pairs = {(item["source"], item["target"]) for item in context["transitions"]}
        self.assertIn(("Text Simplifier", "Text Summarizer"), transition_pairs)
        self.assertTrue(any("Text Simplifier" in motif["tasks"] for motif in context["motifs"]))

    def test_memory_score_rewards_matching_transition(self) -> None:
        context = self.retriever.retrieve(
            "Transcribe the audio, add effects, and render a waveform image.",
            detected_actions=["transcribe", "audio_effect", "waveform"],
        )
        matching_nodes = [
            {"task": "Audio-to-Text"},
            {"task": "Audio Effects"},
            {"task": "Audio-to-Image"},
        ]
        mismatched_nodes = [
            {"task": "Audio Effects"},
            {"task": "Audio-to-Text"},
            {"task": "Audio-to-Image"},
        ]

        matching_score = score_workflow_with_retrieval_context(matching_nodes, context)
        mismatched_score = score_workflow_with_retrieval_context(mismatched_nodes, context)

        self.assertGreater(matching_score["bonus"] + matching_score["penalty"], mismatched_score["bonus"] + mismatched_score["penalty"])

    def test_memory_score_uses_dependency_paths_for_dag_candidates(self) -> None:
        records = [
            {
                "id": "dag-1",
                "type": "dag",
                "instruction": "Simplify the article, summarize it, generate topics, then search using the topics.",
                "tool_nodes": [
                    {"task": "Text Simplifier", "arguments": ["article"]},
                    {"task": "Text Summarizer", "arguments": ["<node-0>"]},
                    {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
                    {"task": "Topic Generator", "arguments": ["<node-1>"]},
                    {"task": "Text Search", "arguments": ["<node-3>"]},
                ],
                "tool_links": [
                    {"source": "Text Simplifier", "target": "Text Summarizer"},
                    {"source": "Text Summarizer", "target": "Keyword Extractor"},
                    {"source": "Text Summarizer", "target": "Topic Generator"},
                    {"source": "Topic Generator", "target": "Text Search"},
                ],
            }
        ]
        memory = WorkflowMemoryIndex.build_from_taskbench_records(records, source_name="unit-test")
        retriever = WorkflowMemoryRetriever(memory)
        context = retriever.retrieve(
            "Simplify the article, summarize it, generate topics, then search using the topics.",
            detected_actions=["simplify", "summarize", "keywords", "topic", "retrieval"],
        )
        candidate_nodes = [
            {"task": "Text Simplifier", "arguments": ["article"]},
            {"task": "Text Summarizer", "arguments": ["<node-0>"]},
            {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
            {"task": "Topic Generator", "arguments": ["<node-1>"]},
            {"task": "Text Search", "arguments": ["<node-3>"]},
        ]

        score = score_workflow_with_retrieval_context(candidate_nodes, context)

        self.assertGreater(score["transition_bonus"], 0.0)
        self.assertGreater(score["motif_bonus"], 0.0)

    def test_agent_prompt_includes_retrieved_workflow_priors(self) -> None:
        agent = PipelineOrchestratorAgent.__new__(PipelineOrchestratorAgent)
        agent.registry = SkillRegistry(SKILLS_ROOT)
        agent._workflow_retriever = self.retriever
        agent._workflow_retrieval_cache = {}
        agent._tool_graph_planner = None
        agent._skill_to_tool_graph_name = {}
        agent._tool_graph_alias_to_skill = {}

        prompt = agent._build_plan_prompt(
            "Please simplify this article and search for related topics on the web."
        )

        self.assertIn("Retrieved workflow priors from aggregated workflow memory:", prompt)
        self.assertIn("Frequent path motif:", prompt)

    def test_format_prompt_block_is_empty_without_context(self) -> None:
        self.assertEqual(format_workflow_memory_prompt_block({}), "")


if __name__ == "__main__":
    unittest.main()
