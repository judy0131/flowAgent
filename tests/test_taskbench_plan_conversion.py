import tempfile
import unittest
import argparse
from pathlib import Path
from unittest.mock import patch

from taskbench.pipelineOrchastration.run_minimal_rollback_experiment import _build_runner_args
from taskbench.pipelineOrchastration.run_with_pipeline_agent_base import (
    _classify_case_failure,
    _open_prediction_output,
)
from taskbench.pipelineOrchastration.run_with_pipeline_agent_openAI import (
    _build_prediction_record,
    _convert_plan_to_taskbench_result,
    _resolve_existing_file,
    build_parser,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestTaskbenchPlanConversion(unittest.TestCase):
    def test_build_parser_defaults_to_openai_gpt54_xhigh_profile(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model_name, "gpt-5.4-xhigh")
        self.assertEqual(args.planning_mode, "multi")
        self.assertEqual(args.llm_config_path, "configs/openai_gpt54_xhigh.json")

    def test_build_parser_accepts_llm_profile_and_config_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--llm_profile", "gpt4", "--llm_config_path", "llm_profiles.json"])

        self.assertEqual(args.llm_profile, "gpt4")
        self.assertEqual(args.llm_config_path, "llm_profiles.json")

    def test_build_parser_accepts_workflow_memory_path(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--workflow_memory_path", "taskbench/data_multimedia/workflow_memory.json"])

        self.assertEqual(args.workflow_memory_path, "taskbench/data_multimedia/workflow_memory.json")

    def test_resolve_existing_file_finds_repo_relative_llm_config_from_nested_cwd(self) -> None:
        nested_cwd = PROJECT_ROOT / "taskbench" / "pipelineOrchastration"
        with patch.object(Path, "cwd", return_value=nested_cwd):
            resolved = _resolve_existing_file("configs/openai.json", label="llm_config_path")

        self.assertEqual(resolved, PROJECT_ROOT / "configs" / "openai.json")

    def test_resource_conversion_reads_workflow_nodes_and_links(self) -> None:
        plan = {
            "task_steps": [
                "Step 1: Call Text Simplifier with arg1=Climate change and its effect on polar bears.",
                "Step 2: Call Text Grammar Checker with arg1=<node-0>.",
                "Step 3: Call Topic Generator with arg1=<node-1>.",
                "Step 4: Call Text-to-Image with arg1=<node-1>.",
            ],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["Climate change and its effect on polar bears"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-0>"]},
                {"task": "Topic Generator", "arguments": ["<node-1>"]},
                {"task": "Text-to-Image", "arguments": ["<node-1>"]},
            ],
            "task_links": [
                {"source": "Text Simplifier", "target": "Text Grammar Checker"},
                {"source": "Text Grammar Checker", "target": "Topic Generator"},
                {"source": "Text Grammar Checker", "target": "Text-to-Image"},
            ],
        }

        result = _convert_plan_to_taskbench_result(
            plan=plan,
            tool_names=[
                "Text Simplifier",
                "Text Grammar Checker",
                "Topic Generator",
                "Text-to-Image",
            ],
            dependency_type="resource",
            tool_map_override={},
            link_mode="explicit_only",
        )

        self.assertEqual(result["task_nodes"][0]["arguments"], ["Climate change and its effect on polar bears"])
        self.assertEqual(result["task_nodes"][1]["arguments"], ["<node-0>"])
        self.assertEqual(result["task_nodes"][2]["arguments"], ["<node-1>"])
        self.assertEqual(result["task_nodes"][3]["arguments"], ["<node-1>"])
        self.assertEqual(
            result["task_links"],
            [
                {"source": "Text Simplifier", "target": "Text Grammar Checker"},
                {"source": "Text Grammar Checker", "target": "Topic Generator"},
                {"source": "Text Grammar Checker", "target": "Text-to-Image"},
            ],
        )
        self.assertIn("arg1=<node-0>", result["task_steps"][1])
        self.assertIn("arg1=<node-1>", result["task_steps"][2])
        self.assertIn("arg1=<node-1>", result["task_steps"][3])

    def test_temporal_conversion_builds_links_from_workflow_refs(self) -> None:
        plan = {
            "task_steps": [
                "Step 1: Call Video-to-Audio with arg1=example.mp4.",
                "Step 2: Call Audio Noise Reduction with arg1=<node-0>.",
                "Step 3: Call Audio Effects with arg2=add reverb effect, arg1=<node-1>.",
            ],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]},
                {"task": "Audio Effects", "arguments": ["add reverb effect", "<node-1>"]},
            ],
            "task_links": [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        }

        result = _convert_plan_to_taskbench_result(
            plan=plan,
            tool_names=["Video-to-Audio", "Audio Noise Reduction", "Audio Effects"],
            dependency_type="temporal",
            tool_map_override={},
            link_mode="explicit_only",
        )

        self.assertEqual(
            result["task_links"],
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        )
        self.assertEqual(
            result["task_nodes"][1]["arguments"],
            [{"name": "arg1", "value": "<node-0>"}],
        )
        self.assertEqual(
            result["task_nodes"][2]["arguments"],
            [
                {"name": "arg1", "value": "add reverb effect"},
                {"name": "arg2", "value": "<node-1>"},
            ],
        )

    def test_build_prediction_record_keeps_only_minimal_eval_result_and_top_level_tool_fields(self) -> None:
        taskbench_result = {
            "task_steps": ["Step 1: Call Video-to-Audio with arg1=example.mp4."],
            "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}],
            "task_links": [],
        }

        result = _build_prediction_record(
            sid="16097613",
            instruction="Extract audio from example.mp4.",
            taskbench_result=taskbench_result,
        )

        self.assertEqual(result["id"], "16097613")
        self.assertEqual(result["instruction"], "Extract audio from example.mp4.")
        self.assertEqual(result["n_tools"], 1)
        self.assertEqual(result["result"], taskbench_result)
        self.assertEqual(
            result["tool_steps"],
            "[\"Step 1: Call Video-to-Audio with arg1=example.mp4.\"]",
        )
        self.assertEqual(
            result["tool_nodes"],
            "[{\"task\": \"Video-to-Audio\", \"arguments\": [\"example.mp4\"]}]",
        )
        self.assertEqual(result["tool_links"], "[]")
        self.assertNotIn("task_steps", result)
        self.assertNotIn("user_request", result)

    def test_open_prediction_output_truncates_when_resume_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "predictions.jsonl"
            output_path.write_text("old\n", encoding="utf-8")

            with _open_prediction_output(output_path, resume=False) as wf:
                wf.write("new\n")

            self.assertEqual(output_path.read_text(encoding="utf-8"), "new\n")

    def test_open_prediction_output_appends_when_resume_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "predictions.jsonl"
            output_path.write_text("old\n", encoding="utf-8")

            with _open_prediction_output(output_path, resume=True) as wf:
                wf.write("new\n")

            self.assertEqual(output_path.read_text(encoding="utf-8"), "old\nnew\n")

    def test_classify_case_failure_marks_workflow_validation_errors(self) -> None:
        error = ValueError(
            "task_nodes[4] invalid dependency grounding: "
            "Text-to-Audio outputs [audio] but Video Voiceover.arg1 expects [video]"
        )

        self.assertEqual(_classify_case_failure(error), "validation_failure")

    def test_classify_case_failure_leaves_non_validation_errors_separate(self) -> None:
        self.assertEqual(_classify_case_failure(RuntimeError("boom")), "other_failure")
        self.assertEqual(_classify_case_failure(ValueError("plain conversion failure")), "other_failure")

    def test_build_runner_args_keeps_continue_on_error_default(self) -> None:
        base_args = argparse.Namespace(
            stop_on_error=False,
            workflow_memory_path=None,
            execution_mode="best",
        )
        group_spec = {
            "planning_mode": "multi",
            "candidate_selection_mode": "rerank",
            "enable_candidate_verifier": True,
            "enable_candidate_repair": True,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
        }

        args = _build_runner_args(
            runner_parser=argparse.ArgumentParser(),
            base_args=base_args,
            group_spec=group_spec,
            prediction_dir="predictions",
            case_ids_file=Path("case_ids.txt"),
            candidate_count=3,
            fixed_temperature=0.0,
        )

        self.assertFalse(args.stop_on_error)


if __name__ == "__main__":
    unittest.main()
