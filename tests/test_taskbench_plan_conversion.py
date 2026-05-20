import tempfile
import unittest
import argparse
import json
from pathlib import Path
from unittest.mock import patch

from taskbench.pipelineOrchastration.run_minimal_rollback_experiment import (
    _default_group_specs,
    _build_runner_args,
    _select_group_specs,
    build_parser as build_rollback_parser,
)
from taskbench.pipelineOrchastration.run_with_pipeline_agent_base import (
    _build_candidate_dump_record,
    _classify_case_failure,
    _open_prediction_output,
    _should_load_workflow_memory_for_run,
    build_parser as build_base_runner_parser,
)
from taskbench.pipelineOrchastration.oracle_candidate_analysis import analyze_candidate_dump
from taskbench.pipelineOrchastration.run_with_pipeline_agent_openAI import (
    _build_prediction_record,
    _convert_plan_to_taskbench_result,
    _resolve_existing_file,
    build_parser,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TestTaskbenchPlanConversion(unittest.TestCase):
    def test_build_parser_defaults_to_openai_runner_profile(self) -> None:
        parser = build_parser()
        args = parser.parse_args([])

        self.assertEqual(args.provider, "openai")
        self.assertEqual(args.model_name, "gpt-4.1")
        self.assertEqual(args.planning_mode, "multi")
        self.assertEqual(args.llm_config_path, "configs/openai.json")

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

    def test_build_candidate_dump_record_keeps_selected_id_and_converted_candidates(self) -> None:
        workflow = {
            "task_steps": [
                "Step 1: Call Text Simplifier with arg1=hello.",
                "Step 2: Call Text Grammar Checker with arg1=<node-0>.",
            ],
            "task_nodes": [
                {"task": "Text Simplifier", "arguments": ["hello"]},
                {"task": "Text Grammar Checker", "arguments": ["<node-0>"]},
            ],
            "task_links": [
                {"source": "Text Simplifier", "target": "Text Grammar Checker"},
            ],
        }
        taskbench_result = _convert_plan_to_taskbench_result(
            plan=workflow,
            tool_names=["Text Simplifier", "Text Grammar Checker"],
            dependency_type="resource",
            tool_map_override={},
            link_mode="explicit_only",
        )
        raw_result = {
            "selected_plan_id": 2,
            "selection_route": "first",
            "candidate_plans": [
                {
                    "id": 1,
                    "workflow": workflow,
                    "score": 1.0,
                    "score_details": {"mock": True},
                    "strategy_name": "original",
                    "strategy_hint": "hint",
                    "sampling_temperature": 0.0,
                    "selection_meta": {"verifier_pass": True},
                    "verification_meta": {"verifier_pass": True},
                    "dependency_check": {"passed": True, "error": None},
                    "repair_meta": {"attempted": False, "applied": False},
                    "edge_grounding_meta": {"mode": "none", "applied": False, "change_count": 0},
                },
                {
                    "id": 2,
                    "workflow": workflow,
                    "score": 2.5,
                    "score_details": {"mock": True},
                    "strategy_name": "explicit",
                    "strategy_hint": "hint2",
                    "sampling_temperature": 0.2,
                    "selection_meta": {"verifier_pass": True},
                    "verification_meta": {"verifier_pass": True},
                    "dependency_check": {"passed": True, "error": None},
                    "repair_meta": {"attempted": False, "applied": False},
                    "edge_grounding_meta": {"mode": "none", "applied": False, "change_count": 0},
                },
            ],
        }

        record = _build_candidate_dump_record(
            sid="case-1",
            instruction="simplify then grammar check",
            taskbench_result=taskbench_result,
            raw_result=raw_result,
            tool_names=["Text Simplifier", "Text Grammar Checker"],
            dependency_type="resource",
            tool_map_override={},
            link_mode="explicit_only",
        )

        self.assertEqual(record["selected_plan_id"], 2)
        self.assertEqual(record["selected_candidate_score"], 2.5)
        self.assertEqual(len(record["candidates"]), 2)
        self.assertEqual(record["candidates"][0]["result"], taskbench_result)
        self.assertEqual(record["candidates"][1]["strategy_name"], "explicit")

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

    def test_build_runner_args_passes_through_edge_grounding_mode(self) -> None:
        base_args = argparse.Namespace(
            stop_on_error=False,
            workflow_memory_path=None,
            execution_mode="best",
        )
        group_spec = {
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "nearest_valid_upstream",
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

        self.assertEqual(args.edge_grounding_mode, "nearest_valid_upstream")

    def test_build_runner_args_passes_through_strict_prompt_and_normalization_flags(self) -> None:
        base_args = argparse.Namespace(
            stop_on_error=False,
            workflow_memory_path=None,
            execution_mode="best",
        )
        group_spec = {
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
            "enable_strict_planning_prompt": True,
            "enable_action_checklist": True,
            "enable_parameter_normalization": True,
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

        self.assertTrue(args.enable_strict_planning_prompt)
        self.assertTrue(args.enable_action_checklist)
        self.assertTrue(args.enable_parameter_normalization)

    def test_build_runner_args_passes_through_save_candidate_pool(self) -> None:
        base_args = argparse.Namespace(
            stop_on_error=False,
            workflow_memory_path=None,
            execution_mode="best",
            save_candidate_pool=True,
        )
        group_spec = {
            "planning_mode": "multi",
            "candidate_selection_mode": "first",
            "enable_candidate_verifier": False,
            "enable_candidate_repair": False,
            "enable_workflow_memory": False,
            "include_original_candidate": True,
            "edge_grounding_mode": "none",
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

        self.assertTrue(args.save_candidate_pool)

    def test_semantic_edge_grounding_modes_request_memory_loading_without_generation_memory(self) -> None:
        for mode in [
            "semantic_edge_scoring",
            "semantic_edge_scoring_h2a",
            "semantic_edge_scoring_h2b",
        ]:
            args = argparse.Namespace(
                enable_workflow_memory=False,
                edge_grounding_mode=mode,
            )

            self.assertTrue(_should_load_workflow_memory_for_run(args), mode)

    def test_nearest_edge_grounding_mode_does_not_request_memory_loading(self) -> None:
        args = argparse.Namespace(
            enable_workflow_memory=False,
            edge_grounding_mode="nearest_valid_upstream",
        )

        self.assertFalse(_should_load_workflow_memory_for_run(args))

    def test_structure_aware_selection_requests_memory_loading(self) -> None:
        args = argparse.Namespace(
            enable_workflow_memory=False,
            edge_grounding_mode="none",
            candidate_selection_mode="structure_aware",
        )

        self.assertTrue(_should_load_workflow_memory_for_run(args))

    def test_base_runner_parser_accepts_structure_aware_selection_mode(self) -> None:
        args = build_base_runner_parser().parse_args(["--candidate_selection_mode", "structure_aware"])

        self.assertEqual(args.candidate_selection_mode, "structure_aware")

    def test_base_runner_parser_accepts_save_candidate_pool(self) -> None:
        args = build_base_runner_parser().parse_args(["--save_candidate_pool"])

        self.assertTrue(args.save_candidate_pool)

    def test_default_group_specs_include_structure_aware_group(self) -> None:
        tag_to_spec = {item["tag"]: item for item in _default_group_specs()}

        self.assertIn("I", tag_to_spec)
        self.assertEqual(tag_to_spec["I"]["candidate_selection_mode"], "structure_aware")
        self.assertTrue(tag_to_spec["I"]["enable_semantic_edge_grounding"])
        self.assertEqual(tag_to_spec["I"]["edge_grounding_mode"], "semantic_edge_scoring")

    def test_default_group_specs_include_strict_prompt_group(self) -> None:
        tag_to_spec = {item["tag"]: item for item in _default_group_specs()}

        self.assertIn("J", tag_to_spec)
        self.assertEqual(tag_to_spec["J"]["candidate_selection_mode"], "first")
        self.assertTrue(tag_to_spec["J"]["include_original_candidate"])
        self.assertTrue(tag_to_spec["J"]["enable_strict_planning_prompt"])
        self.assertTrue(tag_to_spec["J"]["enable_action_checklist"])
        self.assertTrue(tag_to_spec["J"]["enable_parameter_normalization"])

    def test_default_group_specs_include_k_alias_for_b_plus_j_prompt_flags(self) -> None:
        tag_to_spec = {item["tag"]: item for item in _default_group_specs()}

        self.assertIn("K", tag_to_spec)
        self.assertEqual(tag_to_spec["K"]["candidate_selection_mode"], "first")
        self.assertTrue(tag_to_spec["K"]["include_original_candidate"])
        self.assertTrue(tag_to_spec["K"]["enable_strict_planning_prompt"])
        self.assertTrue(tag_to_spec["K"]["enable_action_checklist"])
        self.assertTrue(tag_to_spec["K"]["enable_parameter_normalization"])
        self.assertEqual(tag_to_spec["K"]["edge_grounding_mode"], "none")

    def test_select_group_specs_keeps_requested_order(self) -> None:
        specs = [
            {"tag": "A", "label": "a"},
            {"tag": "F", "label": "f"},
            {"tag": "B", "label": "b"},
        ]

        selected = _select_group_specs(specs, ["F", "B"])

        self.assertEqual([item["tag"] for item in selected], ["F", "B"])

    def test_select_group_specs_rejects_unknown_tag(self) -> None:
        specs = [{"tag": "A", "label": "a"}, {"tag": "F", "label": "f"}]

        with self.assertRaisesRegex(ValueError, "Unknown group_tags"):
            _select_group_specs(specs, ["Z"])

    def test_build_parser_leaves_group_tags_optional_by_default(self) -> None:
        args = build_rollback_parser().parse_args([])

        self.assertIsNone(args.group_tags)

    def test_oracle_candidate_analysis_reports_exact_and_better_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_dir = root / "data_multimedia"
            data_dir.mkdir(parents=True, exist_ok=True)
            save_dir = root / "oracle_analysis"
            candidate_dump_path = root / "candidate_dumps.jsonl"

            gold_row = {
                "id": "1",
                "type": "chain",
                "n_tools": 2,
                "task_steps": [],
                "task_nodes": [
                    {"task": "Text Simplifier", "arguments": ["hello"]},
                    {"task": "Text Grammar Checker", "arguments": ["<node-0>"]},
                ],
                "task_links": [
                    {"source": "Text Simplifier", "target": "Text Grammar Checker"},
                ],
            }
            (data_dir / "data.json").write_text(json.dumps(gold_row, ensure_ascii=False) + "\n", encoding="utf-8")

            selected_result = {
                "task_steps": ["Step 1: Call Text Simplifier with arg1=hello."],
                "task_nodes": [{"task": "Text Simplifier", "arguments": ["hello"]}],
                "task_links": [],
            }
            exact_result = {
                "task_steps": [
                    "Step 1: Call Text Simplifier with arg1=hello.",
                    "Step 2: Call Text Grammar Checker with arg1=<node-0>.",
                ],
                "task_nodes": [
                    {"task": "Text Simplifier", "arguments": ["hello"]},
                    {"task": "Text Grammar Checker", "arguments": ["<node-0>"]},
                ],
                "task_links": [
                    {"source": "Text Simplifier", "target": "Text Grammar Checker"},
                ],
            }
            dump_row = {
                "id": "1",
                "instruction": "simplify then grammar check",
                "selected_plan_id": 1,
                "selected_result": selected_result,
                "selection_route": "first",
                "candidates": [
                    {"id": 1, "strategy_name": "original", "score": 0.1, "result": selected_result},
                    {"id": 2, "strategy_name": "explicit", "score": 0.05, "result": exact_result},
                ],
            }
            candidate_dump_path.write_text(json.dumps(dump_row, ensure_ascii=False) + "\n", encoding="utf-8")

            summary = analyze_candidate_dump(
                data_dir=data_dir,
                candidate_dump_path=candidate_dump_path,
                save_dir=save_dir,
                step_ref_base="one",
            )

            self.assertEqual(summary["case_count"], 1)
            overall = summary["summary_rows"][0]
            self.assertEqual(overall["split"], "overall")
            self.assertEqual(overall["exact_oracle_rate"], 1.0)
            self.assertEqual(overall["node_oracle_rate"], 1.0)
            self.assertEqual(overall["edge_oracle_rate"], 1.0)
            self.assertEqual(overall["oracle_better_rate"], 1.0)
            self.assertTrue((save_dir / "oracle_summary.csv").exists())
            self.assertTrue((save_dir / "oracle_case_details.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
