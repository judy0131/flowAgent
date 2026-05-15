import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskbench.pipelineOrchastration.export_three_tables import (
    _build_badcase_table,
    _build_experiment_table,
    _build_recent_results_rows,
    _infer_model_label,
    _materialize_semantic_graph,
    _write_recent_results_comparison,
)


class TestExportThreeTables(unittest.TestCase):
    def test_materialize_semantic_graph_tolerates_one_based_refs(self) -> None:
        task_names, links, arguments = _materialize_semantic_graph(
            [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio Noise Reduction", "arguments": ["<node-1>"]},
                {"task": "Audio Effects", "arguments": ["<node-2>", "reverb"]},
            ],
            [
                {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                {"source": "Audio Noise Reduction", "target": "Audio Effects"},
            ],
        )

        self.assertEqual(task_names, ["video-to-audio", "audio noise reduction", "audio effects"])
        self.assertEqual(
            links,
            [
                {"source": "video-to-audio", "target": "audio noise reduction"},
                {"source": "audio noise reduction", "target": "audio effects"},
            ],
        )
        self.assertEqual(arguments[1], ["ref:video-to-audio"])
        self.assertEqual(arguments[2], ["lit:reverb", "ref:audio noise reduction"])

    def test_build_badcase_table_ignores_mixed_base_and_arg_order(self) -> None:
        pred_rows = [
            {
                "id": "16097613",
                "result": {
                    "task_nodes": [
                        {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                        {"task": "Audio Noise Reduction", "arguments": ["<node-0>"]},
                        {"task": "Audio Effects", "arguments": ["reverb", "<node-1>"]},
                    ],
                    "task_links": [
                        {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                        {"source": "Audio Noise Reduction", "target": "Audio Effects"},
                    ],
                },
            }
        ]
        gold_rows = {
            "16097613": {
                "id": "16097613",
                "type": "chain",
                "tool_nodes": json.dumps(
                    [
                        {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                        {"task": "Audio Noise Reduction", "arguments": ["<node-1>"]},
                        {"task": "Audio Effects", "arguments": ["<node-2>", "reverb"]},
                    ]
                ),
                "tool_links": json.dumps(
                    [
                        {"source": "Video-to-Audio", "target": "Audio Noise Reduction"},
                        {"source": "Audio Noise Reduction", "target": "Audio Effects"},
                    ]
                ),
            }
        }

        bad_rows, bad_details = _build_badcase_table(pred_rows, gold_rows)

        self.assertEqual(bad_rows, [])
        self.assertEqual(bad_details, [])

    def test_build_badcase_table_hides_normalized_args_in_badcase_output(self) -> None:
        pred_rows = [
            {
                "id": "1",
                "result": {
                    "task_nodes": [
                        {"task": "Video-to-Audio", "arguments": ["example-a.mp4"]},
                    ],
                    "task_links": [],
                },
            }
        ]
        gold_rows = {
            "1": {
                "id": "1",
                "type": "chain",
                "tool_nodes": json.dumps(
                    [
                        {"task": "Video-to-Audio", "arguments": ["example-b.mp4"]},
                    ]
                ),
                "tool_links": json.dumps([]),
            }
        }

        _, bad_details = _build_badcase_table(pred_rows, gold_rows)

        self.assertEqual(len(bad_details), 1)
        mismatches = bad_details[0]["arg_mismatches"]
        self.assertEqual(len(mismatches), 1)
        self.assertNotIn("pred_args_normalized", mismatches[0])
        self.assertNotIn("gold_args_normalized", mismatches[0])

    def test_build_experiment_table_includes_model_column(self) -> None:
        metrics = {
            "overall_overall": {"node_micro_f1_no_matching": 0.91, "link_binary_f1": 0.72},
            "chain_overall": {
                "node_micro_f1_no_matching": 0.9,
                "link_binary_f1": 0.7,
                "edit_distance": 0.16,
            },
            "dag_overall": {"node_micro_f1_no_matching": 0.88, "link_binary_f1": 0.75},
        }

        rows = _build_experiment_table(
            metrics,
            llm_label="FlowAgent(base)",
            domain_label="Multimedia Tool",
            model_label="gpt-5.4-xhigh",
        )

        self.assertEqual(rows[0]["LLM"], "FlowAgent(base)")
        self.assertEqual(rows[0]["Model"], "gpt-5.4-xhigh")

    def test_infer_model_label_prefers_prediction_alias_with_explicit_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            pred_dir = Path(tmpdir)
            generic_pred = pred_dir / "pipeline_orchestrator_agent_20260512.json"
            explicit_pred = pred_dir / "pipeline_orchestrator_agent_gpt-5.4-xhigh_20260512.json"
            generic_pred.write_text("{}", encoding="utf-8")
            explicit_pred.write_text("{}", encoding="utf-8")

            model = _infer_model_label(
                llm_stem="pipeline_orchestrator_agent",
                date_suffix="20260512",
                llm_label="FlowAgent(base)",
                pred_file=generic_pred,
            )

        self.assertEqual(model, "gpt-5.4-xhigh")

    def test_build_recent_results_rows_backfills_model_from_summary_tag_and_prediction_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metrics_root = root / "metrics_pipeline_agent"
            output_dir = metrics_root / "three_tables"
            pred_dir = root / "predictions_pipeline_agent"
            output_dir.mkdir(parents=True)
            pred_dir.mkdir(parents=True)

            (pred_dir / "pipeline_orchestrator_agent_gemini-2.5-flash_20260507-1.json").write_text(
                "{}",
                encoding="utf-8",
            )
            (metrics_root / "pipeline_orchestrator_agent_20260507.json").write_text(
                json.dumps(
                    {
                        "overall_overall": {
                            "node_macro_f1_no_matching": 0.8125,
                        }
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (output_dir / "01_experiment_comparison_gemini_20260507.csv").write_text(
                "\n".join(
                    [
                        "Domain,LLM,Node n-F1,Chain n-F1,Chain e-F1,Chain NED,DAG n-F1,DAG e-F1,Overall n-F1,Overall e-F1",
                        "Multimedia Tool,FlowAgent(gemini),90.00,89.00,70.00,15.00,88.00,68.00,90.00,71.00",
                    ]
                ),
                encoding="utf-8-sig",
            )
            (output_dir / "00_summary_gemini_20260507.json").write_text(
                json.dumps(
                    {
                        "pred_file": str(root / "predictions_pipeline_agent" / "pipeline_orchestrator_agent_20260507.json"),
                        "metrics_file": str(root / "metrics_pipeline_agent" / "pipeline_orchestrator_agent_20260507.json"),
                        "badcase_stats": {
                            "total_predictions": 20,
                            "badcase_count": 11,
                            "badcase_rate": 0.55,
                            "node_ok_in_badcase_count": 2,
                            "node_mismatch_in_badcase_count": 9,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            rows = _build_recent_results_rows(metrics_root)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["RunTag"], "gemini_20260507")
        self.assertEqual(rows[0]["Model"], "gemini-2.5-flash")
        self.assertEqual(rows[0]["Badcase Rate"], "55.00")
        self.assertEqual(rows[0]["Overall Node Macro-F1"], "81.25")
        self.assertEqual(rows[0]["Node OK in Badcase"], "2")
        self.assertEqual(rows[0]["Node Mismatch in Badcase"], "9")

    def test_write_recent_results_comparison_falls_back_when_primary_csv_is_locked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            metrics_root = root / "metrics_pipeline_agent"
            output_dir = metrics_root / "three_tables"
            pred_dir = root / "predictions_pipeline_agent"
            output_dir.mkdir(parents=True)
            pred_dir.mkdir(parents=True)

            (pred_dir / "pipeline_orchestrator_agent_gpt-5.4-xhigh_20260512.json").write_text("{}", encoding="utf-8")
            (metrics_root / "pipeline_orchestrator_agent_20260512.json").write_text(
                json.dumps({"overall_overall": {"node_macro_f1_no_matching": 0.8}}, ensure_ascii=False),
                encoding="utf-8",
            )
            (output_dir / "01_experiment_comparison_20260512.csv").write_text(
                "\n".join(
                    [
                        "Domain,LLM,Model,Node n-F1,Chain n-F1,Chain e-F1,Chain NED,DAG n-F1,DAG e-F1,Overall n-F1,Overall e-F1",
                        "Multimedia Tool,FlowAgent(test),gpt-5.4-xhigh,91.00,90.00,70.00,15.00,88.00,68.00,91.00,71.00",
                    ]
                ),
                encoding="utf-8-sig",
            )
            (output_dir / "00_summary_20260512.json").write_text(
                json.dumps(
                    {
                        "pred_file": str(pred_dir / "pipeline_orchestrator_agent_20260512.json"),
                        "metrics_file": str(metrics_root / "pipeline_orchestrator_agent_20260512.json"),
                        "badcase_stats": {
                            "total_predictions": 20,
                            "badcase_count": 10,
                            "badcase_rate": 0.5,
                            "node_ok_in_badcase_count": 4,
                            "node_mismatch_in_badcase_count": 6,
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            primary_name = str(metrics_root / "00_recent_results_comparison.csv")
            real_open = Path.open

            def fake_open(path_obj: Path, *args, **kwargs):
                if str(path_obj) == primary_name and args and args[0] == "w":
                    raise PermissionError("locked")
                return real_open(path_obj, *args, **kwargs)

            with patch.object(Path, "open", new=fake_open):
                output_path = _write_recent_results_comparison(metrics_root)
                self.assertIsNotNone(output_path)
                self.assertEqual(output_path.name, "00_recent_results_comparison_updated.csv")
                self.assertTrue(output_path.exists())


if __name__ == "__main__":
    unittest.main()
