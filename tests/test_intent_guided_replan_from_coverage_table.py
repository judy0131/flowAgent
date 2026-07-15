import argparse
import json
from pathlib import Path
import tempfile
import unittest

from agent.memory_guided_workflow.experiments.intent_audited_replan_edge_repair import (
    acquire_output_lock,
    build_dag_self_repair_prompt,
    build_output_row,
    evaluate_ablation_stages_with_taskbench,
    build_replan_prompt,
    build_temporal_chain_prior_links,
    build_temporal_edge_only_replan_prompt,
    build_tool_catalog,
    extract_planner_json_object,
    fallback_payload_from_unparsed_replan_output,
    infer_taskbench_dependency_type,
    graph_constrained_repair_workflow,
    is_retryable_replan_failure,
    latest_rows_by_id,
    load_resume_results,
    prepare_output_files,
    release_output_lock,
    repair_workflow_dag,
    run_one_row,
    select_relevant_tools,
    split_intent_hint_coverage,
    split_terms,
    split_tool_hint_coverage,
    should_run_temporal_edge_only_replan,
    strip_temporal_link_repair_from_metric_row,
    summarize_repairs,
    tool_hint_covered_by_model,
    validate_workflow_dag,
    write_taskbench_eval_json,
)
from agent.memory_guided_workflow.utils import extract_json_object


class TestIntentGuidedReplanFromCoverageTable(unittest.TestCase):
    def test_extract_json_object_accepts_vicuna_markdown_escaped_keys(self) -> None:
        payload = extract_json_object(
            ' {\n'
            '"replan\\_decision": "KEEP\\_ORIGINAL",\n'
            '"hint\\_assessment": [{"hint": "Video Search", "status": "already\\_covered"}],\n'
            '"replanned\\_workflow": {"task\\_nodes": [], "task\\_links": []}\n'
            '}'
        )

        self.assertEqual(payload["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(payload["hint_assessment"][0]["status"], "already_covered")
        self.assertEqual(payload["replanned_workflow"]["task_nodes"], [])

    def test_extract_json_object_accepts_missing_final_object_brace(self) -> None:
        payload = extract_json_object(
            '{"replan\\_decision": "KEEP\\_ORIGINAL", '
            '"replanned\\_workflow": {"task\\_nodes": [], "task\\_links": []}'
        )

        self.assertEqual(payload["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(payload["replanned_workflow"]["task_links"], [])

    def test_extract_json_object_uses_first_json_object_before_extra_data(self) -> None:
        payload = extract_json_object(
            '{"replan_decision": "KEEP_ORIGINAL", "replanned_workflow": {}} '
            'Additional explanation that should be ignored.'
        )

        self.assertEqual(payload["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(payload["replanned_workflow"], {})

    def test_extract_planner_json_repairs_missing_object_opener_in_change_array(self) -> None:
        payload, repaired = extract_planner_json_object(
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"original_tool_changes":[{"tool":"Sentence Similarity","action":"keep"},'
            '"tool":"Token Classification","action":"keep"}],'
            '"replanned_workflow":{"task_nodes":[{"id":"node-0","task":"Sentence Similarity","arguments":["x"]}],'
            '"task_links":[]}}'
        )

        self.assertTrue(repaired)
        self.assertEqual(payload["original_tool_changes"][1]["tool"], "Token Classification")

    def test_extract_planner_json_repairs_malformed_task_node_array(self) -> None:
        payload, repaired = extract_planner_json_object(
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":['
            '{"id":"node-0","task":"Visual Question Answering","arguments":["example.jpg"]},'
            '"node-1","task":"Conversational","arguments(["<node-0>"]},'
            '{"id":"node-2","task":"Sentence Similarity","arguments":["<node-1>"]},'
            '],"task_links":[]}}'
        )

        self.assertTrue(repaired)
        nodes = payload["replanned_workflow"]["task_nodes"]
        self.assertEqual(nodes[1]["id"], "node-1")
        self.assertEqual(nodes[1]["arguments"], ["<node-0>"])
        self.assertEqual(nodes[2]["task"], "Sentence Similarity")

    def test_extract_planner_json_repairs_node_keyed_task_node_entry(self) -> None:
        payload, repaired = extract_planner_json_object(
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":['
            '{"id":"node-0","task":"Document Question Answering","arguments":["doc.pdf"]},'
            '"node-1":{"id":"node-1","task":"Question Answering","arguments":["<node-0>"]}'
            '],"task_links":[]}}'
        )

        self.assertTrue(repaired)
        self.assertEqual(payload["replanned_workflow"]["task_nodes"][1]["task"], "Question Answering")

    def test_extract_planner_json_repairs_missing_outer_object_brace(self) -> None:
        payload, repaired = extract_planner_json_object(
            '{"replan_decision":"ADD_MISSING_TOOLS",'
            '"replanned_workflow":{"task_nodes":[],"task_links":[]}'
        )

        self.assertTrue(repaired)
        self.assertEqual(payload["replan_decision"], "ADD_MISSING_TOOLS")
        self.assertEqual(payload["replanned_workflow"]["task_links"], [])

    def test_fallback_accepts_unparsed_visible_keep_original_only(self) -> None:
        previous = {"task_nodes": [{"task": "Text Search", "arguments": ["query"]}], "task_links": []}

        payload = fallback_payload_from_unparsed_replan_output(
            '{"replan\\_decision":"KEEP\\_ORIGINAL","reason":"truncated',
            previous,
        )

        self.assertEqual(payload["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(payload["replanned_workflow"], previous)

        self.assertEqual(
            {},
            fallback_payload_from_unparsed_replan_output(
                '{"replan\\_decision":"REPLACE\\_WRONG\\_TOOLS","reason":"truncated',
                previous,
            ),
        )

    def multimedia_tool_catalog(self):
        return build_tool_catalog(
            [
                {"tool_id": "Video Downloader", "input_types": ["url"], "output_types": ["video"]},
                {"tool_id": "Video-to-Audio", "input_types": ["video"], "output_types": ["audio"]},
                {"tool_id": "Audio-to-Text", "input_types": ["audio"], "output_types": ["text"]},
                {"tool_id": "Audio Effects", "input_types": ["audio", "text"], "output_types": ["audio"]},
            ]
        )

    def test_prompt_includes_previous_workflow_and_intent_hints(self) -> None:
        previous_workflow = {
            "task_steps": ["Step 1: Extract audio."],
            "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}],
            "task_links": [],
        }

        prompt = build_replan_prompt(
            user_request="Create audio from speech in the video at https://example.com/video.mp4.",
            previous_workflow=previous_workflow,
            previous_tool_summary="Video-to-Audio",
            intent_tool_hint="Video Downloader -> Video-to-Audio",
            intent_hint="DownloadVideoFromURL -> ExtractAudioFromVideo",
            tool_desc=[
                {
                    "tool_id": "Video Downloader",
                    "intent": "DownloadVideoFromURL",
                    "desc": "Downloads a video from a given URL.",
                    "input_types": ["url"],
                    "output_types": ["video"],
                }
            ],
        )

        self.assertIn("previous_task_nodes", prompt)
        self.assertIn("Video-to-Audio", prompt)
        self.assertIn("previous_tools", prompt)
        self.assertIn("intent_tools", prompt)
        self.assertIn("Video Downloader", prompt)
        self.assertIn("intents", prompt)
        self.assertIn("DownloadVideoFromURL", prompt)
        self.assertIn("semantic_tool_families", prompt)
        self.assertIn("ADD_MISSING_TOOLS", prompt)
        self.assertIn("no markdown", prompt)
        self.assertIn("no escaped underscores", prompt)
        self.assertIn("evidence/reason <= 80 chars", prompt)
        self.assertIn("https://example.com/video.mp4", prompt)
        self.assertNotIn("Step 1: Extract audio.", prompt)
        self.assertLess(len(prompt), 3000)

    def test_prompt_accepts_dataset_config_rules_and_variables(self) -> None:
        prompt = build_replan_prompt(
            user_request="Answer the question in this audio file.",
            previous_workflow={"task_nodes": [{"task": "Question Answering", "arguments": ["question"]}], "task_links": []},
            previous_tool_summary="Question Answering",
            intent_tool_hint="Automatic Speech Recognition -> Question Answering",
            intent_hint="TranscribeAudioToText -> AnswerQuestionByRetrieveOrSearch",
            tool_desc=[
                {
                    "tool_id": "Automatic Speech Recognition",
                    "intent": "TranscribeAudioToText",
                    "input_types": ["audio"],
                    "output_types": ["text"],
                },
                {
                    "tool_id": "Question Answering",
                    "intent": "AnswerQuestionByRetrieveOrSearch",
                    "input_types": ["text", "text"],
                    "output_types": ["text"],
                },
            ],
            dataset_prompt_rules=["Transcribe audio before text question answering."],
            dataset_prompt_variables={"dataset": "taskbench/data_huggingface"},
        )

        self.assertIn("Dataset-specific rules from --dataset-config", prompt)
        self.assertIn("Transcribe audio before text question answering.", prompt)
        self.assertIn("dataset_prompt_variables", prompt)
        self.assertIn("taskbench/data_huggingface", prompt)

    def test_prompt_accepts_dataset_config_semantic_tool_families(self) -> None:
        prompt = build_replan_prompt(
            user_request="Write a short text.",
            previous_workflow={"task_nodes": [{"task": "Text Generation", "arguments": ["topic"]}], "task_links": []},
            previous_tool_summary="Text Generation",
            intent_tool_hint="Summarization",
            intent_hint="SummarizeTextToShorterVersion",
            tool_desc=[
                {
                    "tool_id": "Text Generation",
                    "intent": "GenerateTextFromPromptOrIncompleteText",
                    "input_types": ["text"],
                    "output_types": ["text"],
                },
                {
                    "tool_id": "Summarization",
                    "intent": "SummarizeTextToShorterVersion",
                    "input_types": ["text"],
                    "output_types": ["text"],
                },
            ],
            semantic_tool_families={"hf_text_generation": ["Text Generation", "Summarization"]},
        )

        self.assertIn("hf_text_generation", prompt)
        self.assertIn("Text Generation", prompt)
        self.assertIn("Summarization", prompt)
        self.assertNotIn("Article Spinner", prompt)

    def test_dag_self_repair_prompt_includes_validator_context(self) -> None:
        previous = {"task_nodes": [{"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]}], "task_links": []}
        candidate = {
            "task_nodes": [
                {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
            ],
            "task_links": [{"source": "node-0", "target": "node-1"}],
        }
        validation = {
            "errors": ["type_mismatch Video Downloader -> Audio-to-Text"],
            "slot_status": [{"target": 1, "tool": "Audio-to-Text", "missing_slots": ["audio"]}],
        }

        prompt = build_dag_self_repair_prompt(
            user_request="Create text from speech in https://example.com/video.mp4.",
            previous_workflow=previous,
            candidate_workflow=candidate,
            validation=validation,
            coverage_row={"intent tool": "Video-to-Audio -> Audio-to-Text"},
            tool_desc=[
                {"tool_id": "Video Downloader", "input_types": ["url"], "output_types": ["video"]},
                {"tool_id": "Video-to-Audio", "input_types": ["video"], "output_types": ["audio"]},
                {"tool_id": "Audio-to-Text", "input_types": ["audio"], "output_types": ["text"]},
            ],
        )

        self.assertIn("validator_errors", prompt)
        self.assertIn("missing_input_slots", prompt)
        self.assertIn("ADD_TOOL_FOR_MISSING_SLOT", prompt)
        self.assertIn("no markdown", prompt)
        self.assertIn("no escaped underscores", prompt)
        self.assertIn("Video-to-Audio", prompt)

    def test_build_output_row_uses_replan_result_only_when_decision_is_replan(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["x.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_steps": ["Step 1: Download.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "Video Downloader", "target": "Video-to-Audio"}],
            },
            "coverage_assessment": [{"hint": "DownloadVideoFromURL", "covered_by_final": True}],
            "change_summary": "Added missing download.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Video-to-Audio",
                "intent tool": "Video Downloader -> Video-to-Audio",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result_source_generic"], "replan")
        self.assertEqual(row["model_tool_original"], "Video-to-Audio")
        self.assertEqual(row["previous_workflow"], previous)
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Video Downloader")
        self.assertEqual(row["selection_reason"], "accepted_conservative_replan")
        trace = row["agent_trace"]
        self.assertEqual(trace["intent_detector"]["missing_tool_hint"], ["Video Downloader"])
        self.assertEqual(trace["workflow_planner"]["workflow"], previous)
        self.assertEqual(trace["intent_checker"]["status"], "not_covered")
        self.assertEqual(trace["workflow_replanner"]["decision"], "ADD_MISSING_TOOLS")
        self.assertEqual(trace["workflow_replanner"]["result_source"], "replan")
        self.assertEqual(trace["structure_detector"]["status"], "skipped_no_tool_catalog")
        self.assertEqual(trace["workflow_repairer"]["final_result_source"], "replan")
        self.assertFalse([key for key in row if "qwen" in key.lower()])

    def test_dag_validator_accepts_materialized_links_from_arguments(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_task_nodes": [
                {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
            ],
            "reason": "Added missing download.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request", "model tool": "Video-to-Audio", "intent tool": "Video Downloader"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "passed")
        self.assertEqual(row["result"]["task_links"], [{"source": "Video Downloader", "target": "Video-to-Audio"}])
        self.assertIn("task_links_missing", row["dag_validation_warnings"][0])

    def test_dag_validator_rejects_type_mismatch(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added transcription.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request", "model tool": "Video Downloader", "intent tool": "Audio-to-Text"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["dag_replan_result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "failed")
        self.assertTrue(any(op["action"] == "drop_type_mismatch_edge" for op in row["dag_repair_operations"]))
        self.assertEqual(
            1,
            sum(1 for op in row["dag_repair_operations"] if op["action"] == "drop_type_mismatch_edge"),
        )

    def test_dag_validator_rejects_invalid_explicit_edge_without_crashing(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": ["<node-99>"]},
                ],
                "task_links": [{"source": "node-99", "target": "node-1"}],
            },
            "reason": "Invalid edge from model output.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "download https://example.com/video.mp4 and extract audio", "model tool": "Video Downloader", "intent tool": "Video-to-Audio"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "repaired_passed")
        self.assertTrue(any("invalid_edge node-99->node-1" in warning for warning in row["dag_validation_warnings"]))

    def test_planner_dag_self_repair_can_add_tool_for_missing_slot(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added transcription.",
        }

        def fake_runner(**_kwargs):
            payload = {
                "repair_decision": "ADD_TOOL_FOR_MISSING_SLOT",
                "repaired_workflow": {
                    "task_nodes": [
                        {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                        {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                        {"task": "Audio-to-Text", "arguments": ["<node-1>"]},
                    ],
                    "task_links": [
                        {"source": "node-0", "target": "node-1"},
                        {"source": "node-1", "target": "node-2"},
                    ],
                },
                "reason": "Video-to-Audio provides the missing audio slot.",
            }
            return {"payload": payload, "raw_output": json.dumps(payload)}

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "transcribe https://example.com/video.mp4", "model tool": "Video Downloader", "intent tool": "Audio-to-Text"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
            dag_self_repair_runner=fake_runner,
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "planner_self_repaired_passed")
        self.assertEqual(row["planner_dag_self_repair_status"], "accepted")
        self.assertEqual([node["task"] for node in row["result"]["task_nodes"]], ["Video Downloader", "Video-to-Audio", "Audio-to-Text"])

    def test_planner_dag_self_repair_rejects_removing_known_tool(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added transcription.",
        }

        def fake_runner(**_kwargs):
            payload = {
                "repair_decision": "REMOVE_INVALID_TOOL",
                "repaired_workflow": {
                    "task_nodes": [{"task": "Audio-to-Text", "arguments": ["https://example.com/video.mp4"]}],
                    "task_links": [],
                },
                "reason": "Remove the downloader.",
            }
            return {"payload": payload, "raw_output": json.dumps(payload)}

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "transcribe https://example.com/video.mp4", "model tool": "Video Downloader", "intent tool": "Audio-to-Text"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
            dag_self_repair_runner=fake_runner,
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["planner_dag_self_repair_status"], "rejected")
        self.assertTrue(any("remove_invalid_tool_removed_known_tools" in warning for warning in row["warnings"]))

    def test_dag_repair_fills_missing_text_slot_from_request(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                    {"task": "Audio Effects", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added audio effects.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "add reverb to the extracted audio", "model tool": "Video-to-Audio", "intent tool": "Audio Effects"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "repaired_passed")
        self.assertTrue(any(op["action"] == "fill_literal_slot_from_request" for op in row["dag_repair_operations"]))
        self.assertIn("add reverb", row["result"]["task_nodes"][1]["arguments"][1])

    def test_dag_repair_does_not_parse_contraction_as_quote(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                    {"task": "Audio Effects", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added audio effects.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "I'm adding reverb to the extracted audio.", "model tool": "Video-to-Audio", "intent tool": "Audio Effects"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["dag_validation_status"], "repaired_passed")
        self.assertIn("I'm adding reverb", row["result"]["task_nodes"][1]["arguments"][1])

    def test_dag_repair_syncs_task_link_without_argument_ref(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": []},
                ],
                "task_links": [{"source": "node-0", "target": "node-1"}],
            },
            "reason": "Added download.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "download https://example.com/video.mp4 and extract audio", "model tool": "Video-to-Audio", "intent tool": "Video Downloader"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=self.multimedia_tool_catalog(),
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["dag_validation_status"], "repaired_passed")
        self.assertEqual(row["result"]["task_nodes"][1]["arguments"], ["<node-0>"])

    def test_dag_repair_uses_tool_graph_to_choose_between_multiple_sources(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Source A", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Source B", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Target", "input_types": ["text"], "output_types": ["text"]},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "Source A", "arguments": []},
                {"task": "Source B", "arguments": []},
                {"task": "Target", "arguments": []},
            ],
            "task_links": [],
        }
        transition_index = {
            ("source a", "target"): 0.1,
            ("source b", "target"): 0.9,
        }

        repaired = repair_workflow_dag(
            workflow,
            user_request="",
            tool_catalog=tool_catalog,
            transition_index=transition_index,
        )

        self.assertEqual(repaired["status"], "repaired")
        self.assertEqual(repaired["workflow"]["task_links"], [{"source": "Source B", "target": "Target"}])
        self.assertTrue(
            any(
                op["action"] == "add_tool_graph_preferred_edge"
                and op["source"] == 1
                and op["target"] == 2
                and op["transition_probability"] == 0.9
                for op in repaired["operations"]
            )
        )

    def test_dag_repair_does_not_choose_ambiguous_multiple_sources_without_tool_graph(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Source A", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Source B", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Target", "input_types": ["text"], "output_types": ["text"]},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "Source A", "arguments": []},
                {"task": "Source B", "arguments": []},
                {"task": "Target", "arguments": []},
            ],
            "task_links": [],
        }

        repaired = repair_workflow_dag(workflow, user_request="", tool_catalog=tool_catalog, transition_index={})

        self.assertEqual(repaired["status"], "unchanged")
        self.assertEqual(repaired["workflow"]["task_links"], [])

    def test_temporal_validation_allows_links_without_node_ref_arguments(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "consult_lawyer_online"},
                {"tool_id": "pay_for_credit_card"},
                {"tool_id": "share_by_social_network"},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "consult_lawyer_online", "arguments": [{"name": "lawyer", "value": "John Doe"}]},
                {"task": "pay_for_credit_card", "arguments": [{"name": "credit_card", "value": "Visa 1234"}]},
                {"task": "share_by_social_network", "arguments": [{"name": "social_network", "value": "Twitter"}]},
            ],
            "task_links": [
                {"source": "consult_lawyer_online", "target": "pay_for_credit_card"},
                {"source": "pay_for_credit_card", "target": "share_by_social_network"},
            ],
        }

        validation = validate_workflow_dag(workflow, tool_catalog=tool_catalog, dependency_type="temporal")
        repaired = repair_workflow_dag(
            workflow,
            user_request="",
            tool_catalog=tool_catalog,
            transition_index={},
            dependency_type="temporal",
        )

        self.assertEqual(validation["status"], "passed")
        self.assertEqual(repaired["status"], "unchanged")
        self.assertEqual(repaired["workflow"]["task_nodes"][1]["arguments"], [{"name": "credit_card", "value": "Visa 1234"}])
        self.assertEqual(
            repaired["workflow"]["task_links"],
            [
                {"source": "consult_lawyer_online", "target": "pay_for_credit_card"},
                {"source": "pay_for_credit_card", "target": "share_by_social_network"},
            ],
        )

    def test_temporal_graph_repair_keeps_existing_links_without_argument_refs(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "consult_lawyer_online"},
                {"tool_id": "pay_for_credit_card"},
                {"tool_id": "share_by_social_network"},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "consult_lawyer_online", "arguments": [{"name": "lawyer", "value": "John Doe"}]},
                {"task": "pay_for_credit_card", "arguments": [{"name": "credit_card", "value": "Visa 1234"}]},
                {"task": "share_by_social_network", "arguments": [{"name": "social_network", "value": "Twitter"}]},
            ],
            "task_links": [{"source": "consult_lawyer_online", "target": "share_by_social_network"}],
        }

        repaired, trace = graph_constrained_repair_workflow(
            workflow,
            tool_catalog=tool_catalog,
            transition_index={},
            dependency_type="temporal",
        )

        self.assertEqual(trace["dependency_type"], "temporal")
        self.assertEqual(
            repaired["task_links"],
            [
                {"source": "consult_lawyer_online", "target": "share_by_social_network"},
            ],
        )
        self.assertEqual(repaired["task_nodes"][1]["arguments"], [{"name": "credit_card", "value": "Visa 1234"}])
        self.assertEqual(trace["operations"], [])

    def test_temporal_graph_repair_keep_mode_preserves_existing_dag_links(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "make_video_call"},
                {"tool_id": "book_hotel"},
                {"tool_id": "get_news_for_topic"},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "make_video_call", "arguments": [{"name": "contact", "value": "John"}]},
                {"task": "book_hotel", "arguments": [{"name": "hotel", "value": "Marriott"}]},
                {"task": "get_news_for_topic", "arguments": [{"name": "topic", "value": "travel"}]},
            ],
            "task_links": [
                {"source": "make_video_call", "target": "book_hotel"},
                {"source": "make_video_call", "target": "get_news_for_topic"},
            ],
        }

        repaired, trace = graph_constrained_repair_workflow(
            workflow,
            tool_catalog=tool_catalog,
            transition_index={},
            dependency_type="temporal",
        )

        self.assertEqual(repaired["task_links"], workflow["task_links"])
        self.assertEqual(trace["operations"], [])

    def test_temporal_chain_prior_builds_adjacent_task_links(self) -> None:
        workflow = {
            "task_nodes": [
                {"task": "get_weather", "arguments": [{"name": "location", "value": "Boston"}]},
                {"task": "book_taxi", "arguments": [{"name": "destination", "value": "airport"}]},
                {"task": "send_sms", "arguments": [{"name": "content", "value": "on my way"}]},
            ],
            "task_links": [],
        }

        self.assertEqual(
            build_temporal_chain_prior_links(workflow),
            [
                {"source": "get_weather", "target": "book_taxi"},
                {"source": "book_taxi", "target": "send_sms"},
            ],
        )

    def test_temporal_validation_allows_backward_order_links(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "get_weather"},
                {"tool_id": "book_taxi"},
                {"tool_id": "send_sms"},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "get_weather", "arguments": [{"name": "location", "value": "Boston"}]},
                {"task": "book_taxi", "arguments": [{"name": "destination", "value": "airport"}]},
                {"task": "send_sms", "arguments": [{"name": "content", "value": "bring umbrella"}]},
            ],
            "task_links": [{"source": "send_sms", "target": "get_weather"}],
        }

        validation = validate_workflow_dag(workflow, tool_catalog=tool_catalog, dependency_type="temporal")
        repaired = repair_workflow_dag(
            workflow,
            user_request="",
            tool_catalog=tool_catalog,
            transition_index={},
            dependency_type="temporal",
        )

        self.assertEqual(validation["status"], "passed")
        self.assertEqual(repaired["workflow"]["task_links"], workflow["task_links"])

    def test_resource_validation_still_rejects_backward_order_links(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Target", "input_types": ["text"], "output_types": ["text"]},
                {"tool_id": "Source", "input_types": [], "output_types": ["text"]},
            ]
        )
        workflow = {
            "task_nodes": [
                {"task": "Target", "arguments": ["<node-1>"]},
                {"task": "Source", "arguments": []},
            ],
            "task_links": [{"source": "Source", "target": "Target"}],
        }

        validation = validate_workflow_dag(workflow, tool_catalog=tool_catalog, dependency_type="resource")

        self.assertEqual(validation["status"], "failed")
        self.assertTrue(any("future_or_self_edge" in error for error in validation["errors"]))

    def test_temporal_edge_only_replan_updates_links_without_changing_nodes(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "get_weather"},
                {"tool_id": "book_taxi"},
                {"tool_id": "send_sms"},
            ]
        )
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "get_weather", "arguments": [{"name": "location", "value": "Boston"}]},
                {"task": "book_taxi", "arguments": [{"name": "destination", "value": "airport"}]},
                {"task": "send_sms", "arguments": [{"name": "content", "value": "arriving soon"}]},
            ],
            "task_links": [],
        }
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            payload = {
                "decision": "USE_CHAIN_PRIOR",
                "task_links": kwargs["chain_prior_task_links"],
                "reason": "chain dependencies are sufficient",
            }
            return {"payload": payload, "raw_output": json.dumps(payload)}

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "Check weather, book a taxi, then text me.", "model tool": ""},
            previous_workflow=previous,
            planner_payload={"replan_decision": "KEEP_ORIGINAL", "replanned_workflow": previous},
            raw_planner_output="",
            warnings=[],
            tool_catalog=tool_catalog,
            dependency_type="temporal",
            temporal_chain_prior=True,
            temporal_edge_only_scope="all",
            edge_only_replan_runner=runner,
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(row["result"]["task_nodes"], previous["task_nodes"])
        self.assertEqual(
            row["result"]["task_links"],
            [
                {"source": "get_weather", "target": "book_taxi"},
                {"source": "book_taxi", "target": "send_sms"},
            ],
        )
        self.assertEqual(row["temporal_link_repair"]["status"], "accepted")
        self.assertIn("temporal_edge_only_replan_passed", row["selection_reason"])

    def test_temporal_edge_only_replan_is_not_run_for_resource_mode(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Source", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Target", "input_types": [], "output_types": ["text"]},
            ]
        )
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Source", "arguments": []}, {"task": "Target", "arguments": []}],
            "task_links": [],
        }

        def runner(**kwargs):
            raise AssertionError("resource mode must not run temporal edge-only replan")

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "use both tools", "model tool": ""},
            previous_workflow=previous,
            planner_payload={"replan_decision": "KEEP_ORIGINAL", "replanned_workflow": previous},
            raw_planner_output="",
            warnings=[],
            tool_catalog=tool_catalog,
            dependency_type="resource",
            temporal_chain_prior=True,
            temporal_edge_only_scope="all",
            edge_only_replan_runner=runner,
        )

        self.assertEqual(row["temporal_link_repair"]["status"], "skipped_not_temporal")

    def test_metric_replan_rows_strip_temporal_edge_repair_result(self) -> None:
        pre_repair = {
            "task_nodes": [
                {"task": "get_weather", "arguments": [{"name": "location", "value": "Boston"}]},
                {"task": "book_taxi", "arguments": [{"name": "destination", "value": "airport"}]},
            ],
            "task_links": [],
        }
        post_repair = {
            "task_nodes": pre_repair["task_nodes"],
            "task_links": [{"source": "get_weather", "target": "book_taxi"}],
        }
        row = {
            "id": "case-1",
            "result": post_repair,
            "result_source": "previous_workflow",
            "pre_temporal_link_repair_result": pre_repair,
            "pre_temporal_link_repair_result_source": "previous_workflow",
            "pre_temporal_link_repair_selection_reason": "keep_original",
            "dag_replan_result": pre_repair,
            "temporal_link_repair": {"status": "accepted"},
        }

        stripped = strip_temporal_link_repair_from_metric_row(row)

        self.assertTrue(stripped["metric_temporal_link_repair_removed"])
        self.assertEqual(stripped["result"]["task_nodes"], pre_repair["task_nodes"])
        self.assertEqual(stripped["result"]["task_links"], pre_repair["task_links"])
        self.assertEqual(row["result"], post_repair)

    def test_metric_replan_rows_keep_unapplied_temporal_edge_repair_result(self) -> None:
        workflow = {
            "task_nodes": [{"task": "get_weather", "arguments": []}],
            "task_links": [],
        }
        row = {
            "id": "case-1",
            "result": workflow,
            "dag_replan_result": {"task_nodes": [{"task": "other", "arguments": []}], "task_links": []},
            "temporal_link_repair": {"status": "validation_failed"},
        }

        stripped = strip_temporal_link_repair_from_metric_row(row)

        self.assertNotIn("metric_temporal_link_repair_removed", stripped)
        self.assertEqual(stripped["result"], workflow)

    def test_repair_summary_counts_temporal_edge_repair_changes(self) -> None:
        pre_repair = {
            "task_nodes": [
                {"task": "get_weather", "arguments": []},
                {"task": "book_taxi", "arguments": []},
            ],
            "task_links": [],
        }
        post_repair = {
            "task_nodes": pre_repair["task_nodes"],
            "task_links": [{"source": "get_weather", "target": "book_taxi"}],
        }

        summary = summarize_repairs(
            [
                {
                    "id": "case-1",
                    "result": post_repair,
                    "pre_temporal_link_repair_result": pre_repair,
                    "graph_repair_trace": {"operations": [], "applied": True, "validation_status": "passed"},
                    "temporal_link_repair": {"status": "accepted"},
                }
            ]
        )

        self.assertEqual(summary["changed_rows"], 1)
        self.assertEqual(summary["temporal_changed_rows"], 1)
        self.assertEqual(summary["temporal_status_counts"], {"accepted": 1})

    def test_temporal_edge_only_prompt_and_scope(self) -> None:
        workflow = {
            "task_nodes": [
                {"task": "get_weather", "arguments": [{"name": "location", "value": "Boston"}]},
                {"task": "book_taxi", "arguments": [{"name": "destination", "value": "airport"}]},
            ],
            "task_links": [],
        }

        prompt = build_temporal_edge_only_replan_prompt(
            user_request="Check weather and book a taxi.",
            workflow=workflow,
            original_task_links=[],
            chain_prior_task_links=build_temporal_chain_prior_links(workflow),
            graph_candidates=[],
            scope="all",
        )

        self.assertIn("Repair only task_links", prompt)
        self.assertIn("Do not change task_nodes", prompt)
        self.assertTrue(should_run_temporal_edge_only_replan(workflow, "high-risk")[0])

    def test_keep_original_verifier_rewires_high_risk_keep_row_with_tool_graph(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Source A", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Source B", "input_types": [], "output_types": ["text"]},
                {"tool_id": "Target", "input_types": ["text"], "output_types": ["text"]},
            ]
        )
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Source A", "arguments": []},
                {"task": "Source B", "arguments": []},
                {"task": "Target", "arguments": ["<node-0>"]},
            ],
            "task_links": [{"source": "node-0", "target": "node-2"}],
        }
        planner_payload = {
            "replan_decision": "KEEP_ORIGINAL",
            "replanned_workflow": previous,
            "reason": "keep previous workflow",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "use source b for the target", "model tool": "Source A -> Source B -> Target"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            tool_catalog=tool_catalog,
            transition_index={("source a", "target"): 0.01, ("source b", "target"): 0.9},
            dataset_config={
                "keep_original_graph_repair": {
                    "enabled": True,
                    "risk_threshold": 25,
                    "optional_threshold": 0.05,
                }
            },
        )

        self.assertEqual(row["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(row["result_source"], "keep_original_graph_repair")
        self.assertEqual(row["dag_validation_status"], "keep_original_graph_repaired_passed")
        self.assertEqual(row["result"]["task_nodes"][2]["arguments"], ["<node-1>"])
        self.assertEqual(row["result"]["task_links"], [{"source": "Source B", "target": "Target"}])
        self.assertTrue(row["keep_original_verifier"]["repair_applied"])
        self.assertTrue(
            any(op["action"] == "global_select_graph_edge" and op["source"] == 1 for op in row["dag_repair_operations"])
        )

    def test_build_output_row_rejects_unsafe_deletion(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
            ],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REPLACE_WRONG_TOOLS",
            "replanned_workflow": {
                "task_steps": ["Step 1: Transcribe the video."],
                "task_nodes": [{"task": "Video-to-Text", "arguments": ["example.mp4"]}],
                "task_links": [],
            },
            "hint_assessment": [
                {
                    "intent_tool_or_intent": "Video-to-Text",
                    "status": "equivalent_to_existing",
                    "existing_tool_or_path": "Video-to-Audio -> Audio-to-Text",
                }
            ],
            "change_summary": "Replaced with direct tool.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request", "intent tool": "Video-to-Text"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Video-to-Audio")
        self.assertIn("unsafe_deletion", row["selection_reason"])

    def test_build_output_row_accepts_simplified_replanned_task_nodes(self) -> None:
        previous = {"task_steps": [], "task_nodes": [{"task": "Video-to-Audio", "arguments": ["x.mp4"]}], "task_links": []}
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_task_nodes": [
                {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
            ],
            "reason": "Added missing download.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Video-to-Audio",
                "intent tool": "Video Downloader -> Video-to-Audio",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Video Downloader")
        self.assertEqual(row["change_summary"], "Added missing download.")

    def test_build_output_row_canonicalizes_download_video_to_video_downloader(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Audio-to-Image", "arguments": ["<node-0>"]},
            ],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_task_nodes": [
                {"task": "Download Video", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                {"task": "Audio-to-Image", "arguments": ["<node-1>"]},
            ],
            "reason": "Added missing video download.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Video-to-Audio -> Audio-to-Image",
                "intent tool": "Video Downloader -> Video-to-Audio -> Audio-to-Image",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Video Downloader")

    def test_build_output_row_accepts_justified_replacement(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Keyword Extractor", "arguments": ["<node-0>"]}],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REPLACE_WRONG_TOOLS",
            "replanned_workflow": {
                "task_steps": ["Step 1: Search the translated text online."],
                "task_nodes": [{"task": "Text Search", "arguments": ["<node-0>"]}],
                "task_links": [],
            },
            "original_tool_changes": [
                {
                    "tool": "Keyword Extractor",
                    "action": "replace",
                    "replacement": "Text Search",
                    "evidence_from_request": "The user asks to search, so Keyword Extractor is the wrong tool.",
                }
            ],
            "change_summary": "Replaced wrong extraction step with search.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request", "intent tool": "Text Search"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Text Search")

    def test_build_output_row_rejects_same_semantic_family_replacement(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Article Spinner", "arguments": ["article"]}],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REPLACE_WRONG_TOOLS",
            "replanned_workflow": {
                "task_nodes": [{"task": "Text Paraphraser", "arguments": ["article"]}],
                "task_links": [],
            },
            "original_tool_changes": [
                {
                    "tool": "Article Spinner",
                    "action": "replace",
                    "replacement": "Text Paraphraser",
                    "evidence": "The hint names a more specific tool.",
                }
            ],
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "rewrite this article", "intent tool": "Text Paraphraser"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Article Spinner")
        self.assertEqual(row["selection_reason"], "same_semantic_family_replacement: used previous workflow")
        self.assertTrue(any("same_semantic_family_replacement" in warning for warning in row["warnings"]))

    def test_build_output_row_allows_dataset_config_to_clear_semantic_families(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Article Spinner", "arguments": ["article"]}],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REPLACE_WRONG_TOOLS",
            "replanned_workflow": {
                "task_nodes": [{"task": "Text Paraphraser", "arguments": ["article"]}],
                "task_links": [],
            },
            "original_tool_changes": [
                {
                    "tool": "Article Spinner",
                    "action": "replace",
                    "replacement": "Text Paraphraser",
                    "evidence": "Article Spinner is the wrong tool for this dataset.",
                }
            ],
            "reason": "Dataset config does not mark these tools as equivalent.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "rewrite this article", "intent tool": "Text Paraphraser"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
            semantic_tool_families={},
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Text Paraphraser")

    def test_build_output_row_accepts_replacement_across_semantic_families(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Keyword Extractor", "arguments": ["text"]}],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REPLACE_WRONG_TOOLS",
            "replanned_workflow": {
                "task_steps": ["Step 1: Search the text online."],
                "task_nodes": [{"task": "Text Search", "arguments": ["text"]}],
                "task_links": [],
            },
            "original_tool_changes": [
                {
                    "tool": "Keyword Extractor",
                    "action": "replace",
                    "replacement": "Text Search",
                    "evidence_from_request": "The user asks to search, so Keyword Extractor is the wrong tool.",
                }
            ],
            "reason": "Replaced wrong extraction step with search.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "search this text online", "intent tool": "Text Search"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Text Search")

    def test_build_output_row_accepts_rebuild_with_original_plus_accepted_hint(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["https://example.com/video.mp4"]},
                {"task": "Audio-to-Image", "arguments": ["<node-0>"]},
                {"task": "Image Search (by Image)", "arguments": ["<node-1>"]},
            ],
            "task_links": [],
        }
        planner_payload = {
            "decision": "REBUILD_WITH_ACCEPTED_HINTS",
            "hint_assessment": [
                {"tool": "Video Downloader", "status": "missing_should_add", "evidence": "The request uses a video URL."}
            ],
            "final_tools": ["Video Downloader", "Video-to-Audio", "Audio-to-Image", "Image Search (by Image)"],
            "workflow": {
                "task_nodes": [
                    {"task": "Download Video", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                    {"task": "Audio-to-Image", "arguments": ["<node-1>"]},
                    {"task": "Image Search (by Image)", "arguments": ["<node-2>"]},
                ]
            },
            "reason": "Rebuilt with the accepted download hint.",
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Video-to-Audio -> Audio-to-Image -> Image Search (by Image)",
                "intent tool": "Video Downloader -> Video-to-Audio -> Audio-to-Image -> Image Search (by Image)",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["replan_decision"], "REBUILD_WITH_ACCEPTED_HINTS")
        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(row["result"]["task_nodes"][0]["task"], "Video Downloader")

    def test_build_output_row_rejects_rebuild_with_disallowed_tool(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [{"task": "Text Summarizer", "arguments": ["article"]}],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REBUILD_WITH_ACCEPTED_HINTS",
            "hint_assessment": [
                {"tool": "Article Spinner", "status": "missing_should_add", "evidence": "Article rewrite is required."}
            ],
            "final_tools": ["Text Summarizer", "Article Spinner", "Text-to-Video"],
            "replanned_task_nodes": [
                {"task": "Text Summarizer", "arguments": ["article"]},
                {"task": "Article Spinner", "arguments": ["<node-0>"]},
                {"task": "Text-to-Video", "arguments": ["<node-1>"]},
            ],
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Text Summarizer",
                "intent tool": "Article Spinner",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["selection_reason"], "rebuild_used_disallowed_tools: used previous workflow")

    def test_build_output_row_accepts_rebuild_replacing_existing_path(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                {"task": "Keyword Extractor", "arguments": ["<node-1>"]},
            ],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "REBUILD_WITH_ACCEPTED_HINTS",
            "hint_assessment": [
                {
                    "tool": "Video-to-Text",
                    "status": "replacement_for_existing_path",
                    "evidence": "Video-to-Text replaces the previous video-audio-text path.",
                }
            ],
            "original_tool_changes": [
                {"tool": "Video-to-Audio", "action": "replace", "replacement": "Video-to-Text", "evidence": "replace"},
                {"tool": "Audio-to-Text", "action": "remove", "replacement": "Video-to-Text", "evidence": "remove"},
                {"tool": "Keyword Extractor", "action": "keep"},
            ],
            "final_tools": ["Video-to-Text", "Keyword Extractor"],
            "replanned_task_nodes": [
                {"task": "Video-to-Text", "arguments": ["example.mp4"]},
                {"task": "Keyword Extractor", "arguments": ["<node-0>"]},
            ],
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={
                "user_request": "request",
                "model tool": "Video-to-Audio -> Audio-to-Text -> Keyword Extractor",
                "intent tool": "Video-to-Text -> Keyword Extractor",
            },
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "replan")
        self.assertEqual(
            [node["task"] for node in row["result"]["task_nodes"]],
            ["Video-to-Text", "Keyword Extractor"],
        )

    def test_build_output_row_rejects_redundant_shortcut_addition(self) -> None:
        previous = {
            "task_steps": [],
            "task_nodes": [
                {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
            ],
            "task_links": [],
        }
        planner_payload = {
            "replan_decision": "ADD_MISSING_TOOLS",
            "replanned_workflow": {
                "task_steps": ["Step 1: Extract audio.", "Step 2: Transcribe audio.", "Step 3: Directly transcribe video."],
                "task_nodes": [
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                    {"task": "Audio-to-Text", "arguments": ["<node-0>"]},
                    {"task": "Video-to-Text", "arguments": ["example.mp4"]},
                ],
                "task_links": [],
            },
            "original_tool_changes": [
                {"tool": "Video-to-Audio", "action": "keep"},
                {"tool": "Audio-to-Text", "action": "keep"},
                {
                    "tool": "Video-to-Text",
                    "action": "add_after",
                    "evidence_from_request": "This direct tool is more efficient for the same user intent.",
                },
            ],
        }

        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request", "intent tool": "Video-to-Text"},
            previous_workflow=previous,
            planner_payload=planner_payload,
            raw_planner_output=json.dumps(planner_payload),
            warnings=[],
        )

        self.assertEqual(row["result_source"], "previous_workflow")
        self.assertEqual(row["selection_reason"], "redundant_equivalent_shortcut: used previous workflow")

    def test_prepare_output_files_requires_resume_or_overwrite_for_existing_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_jsonl = Path(temp_dir) / "out.jsonl"
            output_xlsx = Path(temp_dir) / "out.xlsx"
            eval_json = Path(temp_dir) / "eval.json"
            output_jsonl.write_text("{}", encoding="utf-8")
            eval_json.write_text("{}", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                prepare_output_files(output_jsonl, output_xlsx, resume=False, overwrite=False)

            prepare_output_files(output_jsonl, output_xlsx, resume=True, overwrite=False)
            self.assertTrue(output_jsonl.exists())
            self.assertTrue(eval_json.exists())

            prepare_output_files(output_jsonl, output_xlsx, resume=False, overwrite=True, eval_json=eval_json)
            self.assertFalse(output_jsonl.exists())
            self.assertFalse(eval_json.exists())

    def test_write_taskbench_eval_json_keeps_only_id_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "prediction.json"

            write_taskbench_eval_json(
                output,
                [
                    {
                        "id": "case-1",
                        "result": {
                            "task_steps": ["Step 1"],
                            "task_nodes": [{"task": "Text Search", "arguments": ["query"]}],
                            "task_links": [],
                        },
                        "dag_validation_status": "passed",
                    }
                ],
            )

            rows = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(rows, [
            {
                "id": "case-1",
                "result": {
                    "task_steps": ["Step 1"],
                    "task_nodes": [{"task": "Text Search", "arguments": ["query"]}],
                    "task_links": [],
                },
            }
        ])

    def test_write_taskbench_eval_json_strips_temporal_node_refs_from_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_output = Path(temp_dir) / "resource.json"
            temporal_output = Path(temp_dir) / "temporal.json"
            rows = [
                {
                    "id": "case-1",
                    "result": {
                        "task_steps": [],
                        "task_nodes": [
                            {"task": "get_weather", "arguments": [{"name": "city", "value": "Paris"}]},
                            {
                                "task": "set_alarm",
                                "arguments": [
                                    "<node-0>",
                                    "literal",
                                    {"name": "time", "value": "08:00"},
                                ],
                            },
                        ],
                        "task_links": [{"source": "get_weather", "target": "set_alarm"}],
                    },
                }
            ]

            write_taskbench_eval_json(resource_output, rows)
            write_taskbench_eval_json(temporal_output, rows, dependency_type="temporal")

            resource_row = json.loads(resource_output.read_text(encoding="utf-8").splitlines()[0])
            temporal_row = json.loads(temporal_output.read_text(encoding="utf-8").splitlines()[0])

        self.assertEqual(
            resource_row["result"]["task_nodes"][1]["arguments"],
            ["<node-0>", "literal", {"name": "time", "value": "08:00"}],
        )
        self.assertEqual(
            temporal_row["result"]["task_nodes"][1]["arguments"],
            [{"name": "time", "value": "08:00"}],
        )
        self.assertEqual(
            temporal_row["result"]["task_links"],
            [{"source": "get_weather", "target": "set_alarm"}],
        )

    def test_evaluate_ablation_stages_uses_taskbench_evaluate_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            dataset_dir = Path(temp_dir) / "data_demo"
            dataset_dir.mkdir()
            (dataset_dir / "tool_desc.json").write_text(
                json.dumps(
                    {
                        "nodes": [
                            {"id": "Text Search", "output-type": ["text"]},
                            {"id": "Text Summarization", "output-type": ["text"]},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (dataset_dir / "data.json").write_text(
                json.dumps(
                    {
                        "id": "case-1",
                        "type": "chain",
                        "n_tools": 2,
                        "task_steps": ["search", "summarize"],
                        "task_nodes": [
                            {"task": "Text Search", "arguments": ["query"]},
                            {"task": "Text Summarization", "arguments": ["<node-0>"]},
                        ],
                        "task_links": [{"source": "Text Search", "target": "Text Summarization"}],
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            correct_row = {
                "id": "case-1",
                "result": {
                    "task_steps": ["search", "summarize"],
                    "task_nodes": [
                        {"task": "Text Search", "arguments": ["query"]},
                        {"task": "Text Summarization", "arguments": ["<node-0>"]},
                    ],
                    "task_links": [{"source": "Text Search", "target": "Text Summarization"}],
                },
            }
            wrong_row = {
                "id": "case-1",
                "result": {
                    "task_steps": ["search"],
                    "task_nodes": [{"task": "Text Search", "arguments": ["query"]}],
                    "task_links": [],
                },
            }

            (
                metrics,
                stage_prediction_files,
                stage_metric_files,
                prediction_dir,
                metric_dir,
            ) = evaluate_ablation_stages_with_taskbench(
                dataset_dir=dataset_dir,
                eval_prediction_dir=dataset_dir / "replan_reformat_by_self" / "eval_inputs",
                eval_metric_dir=dataset_dir / "replan_reformat_by_self" / "eval_metrics",
                stage_rows={
                    "original": [correct_row],
                    "intent_replan": [wrong_row],
                    "edge_repaired": [correct_row],
                },
            )

            self.assertEqual(prediction_dir, "replan_reformat_by_self/eval_inputs")
            self.assertTrue(metric_dir.endswith("replan_reformat_by_self\\eval_metrics") or metric_dir.endswith("replan_reformat_by_self/eval_metrics"))
            self.assertTrue(Path(stage_prediction_files["original"]).exists())
            self.assertTrue(Path(stage_metric_files["original"]).exists())
            self.assertEqual(metrics["original"]["node_micro_f1"], 1.0)
            self.assertEqual(metrics["original"]["edge_micro_f1"], 1.0)
            self.assertEqual(metrics["original"]["ned"], 0.0)
            self.assertEqual(metrics["original"]["print_evaluate_metrics_table_row"]["n_f1"], 1.0)
            self.assertLess(metrics["intent_replan"]["node_micro_f1"], 1.0)
            self.assertEqual(metrics["edge_repaired"]["taskbench_metric"]["link_binary_f1"], 1.0)
            self.assertTrue(stage_prediction_files["original"].endswith("original.json"))
            self.assertTrue(stage_metric_files["original"].endswith("original.json"))

    def test_infer_taskbench_dependency_type_from_tool_desc_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            resource_dir = Path(temp_dir) / "data_resource"
            temporal_dir = Path(temp_dir) / "data_temporal"
            resource_dir.mkdir()
            temporal_dir.mkdir()
            (resource_dir / "tool_desc.json").write_text(
                json.dumps({"nodes": [{"id": "Text Search", "input-type": ["text"], "output-type": ["text"]}]}),
                encoding="utf-8",
            )
            (temporal_dir / "tool_desc.json").write_text(
                json.dumps({"nodes": [{"id": "get_weather", "parameters": [{"name": "location"}]}]}),
                encoding="utf-8",
            )

            self.assertEqual(infer_taskbench_dependency_type(resource_dir), "resource")
            self.assertEqual(infer_taskbench_dependency_type(temporal_dir), "temporal")

    def test_output_lock_rejects_second_writer(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_jsonl = Path(temp_dir) / "out.jsonl"
            lock_path = acquire_output_lock(output_jsonl)
            try:
                with self.assertRaises(RuntimeError):
                    acquire_output_lock(output_jsonl)
            finally:
                release_output_lock(lock_path)
            self.assertFalse(lock_path.exists())

    def test_split_terms(self) -> None:
        self.assertEqual(split_terms("A -> B, C\nD"), ["A", "B", "C", "D"])

    def test_split_hint_coverage_marks_partial_tool_and_intent_hints(self) -> None:
        tool_catalog = build_tool_catalog(
            [
                {"tool_id": "Video Downloader", "intent": "DownloadVideoFromURL"},
                {"tool_id": "Video-to-Audio", "intent": "ExtractAudioFromVideo"},
            ]
        )

        tool_coverage = split_tool_hint_coverage(
            "Video-to-Audio",
            "Video Downloader -> Video-to-Audio",
        )
        intent_coverage = split_intent_hint_coverage(
            "Video-to-Audio",
            "DownloadVideoFromURL -> ExtractAudioFromVideo",
            tool_catalog,
        )

        self.assertEqual(tool_coverage["covered"], ["Video-to-Audio"])
        self.assertEqual(tool_coverage["missing"], ["Video Downloader"])
        self.assertEqual(intent_coverage["covered"], ["ExtractAudioFromVideo"])
        self.assertEqual(intent_coverage["missing"], ["DownloadVideoFromURL"])

    def test_replan_prompt_marks_partial_hint_context(self) -> None:
        prompt = build_replan_prompt(
            user_request="Create audio from speech in the video at https://example.com/video.mp4.",
            previous_workflow={
                "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}],
                "task_links": [],
            },
            previous_tool_summary="Video-to-Audio",
            intent_tool_hint="Video Downloader -> Video-to-Audio",
            intent_hint="DownloadVideoFromURL -> ExtractAudioFromVideo",
            tool_desc=[
                {"tool_id": "Video Downloader", "intent": "DownloadVideoFromURL"},
                {"tool_id": "Video-to-Audio", "intent": "ExtractAudioFromVideo"},
            ],
        )

        self.assertIn('"covered_intent_tools":["Video-to-Audio"]', prompt)
        self.assertIn('"missing_intent_tools":["Video Downloader"]', prompt)
        self.assertIn('"covered_intents":["ExtractAudioFromVideo"]', prompt)
        self.assertIn('"missing_intents":["DownloadVideoFromURL"]', prompt)
        self.assertIn("must not be duplicated", prompt)

    def test_replan_prompt_has_temporal_mode_without_node_refs(self) -> None:
        prompt = build_replan_prompt(
            user_request="Book a lawyer consultation, pay by Visa, then share it on Twitter.",
            previous_workflow={
                "task_nodes": [{"task": "consult_lawyer_online", "arguments": [{"name": "lawyer", "value": "John Doe"}]}],
                "task_links": [],
            },
            previous_tool_summary="consult_lawyer_online",
            intent_tool_hint="consult_lawyer_online -> pay_for_credit_card -> share_by_social_network",
            intent_hint="ConsultLawyerOnline -> PayCreditCard -> ShareBySocialNetwork",
            tool_desc=[
                {
                    "tool_id": "consult_lawyer_online",
                    "intent": "ConsultLawyerOnline",
                    "parameters": [{"name": "issue"}, {"name": "lawyer"}],
                },
                {
                    "tool_id": "pay_for_credit_card",
                    "intent": "PayCreditCard",
                    "parameters": [{"name": "credit_card"}],
                },
                {
                    "tool_id": "share_by_social_network",
                    "intent": "ShareBySocialNetwork",
                    "parameters": [{"name": "content"}, {"name": "social_network"}],
                },
            ],
            dependency_type="temporal",
        )

        self.assertIn("Dependency mode: temporal", prompt)
        self.assertIn("Do not use <node-i>", prompt)
        self.assertIn('"parameters":[{"name":"credit_card"}]', prompt)
        self.assertIn('"arguments":[{"name":"parameter_name","value":"parameter_value"}]', prompt)
        self.assertIn('"source":"task name","target":"task name"', prompt)

    def test_tool_hint_covered_by_model_ignores_order_and_allows_model_superset(self) -> None:
        self.assertTrue(
            tool_hint_covered_by_model(
                "Video-to-Audio -> Video Downloader",
                "video downloader, video to audio",
            )
        )
        self.assertTrue(
            tool_hint_covered_by_model(
                "Text Downloader -> Keyword Extractor -> Voice Changer -> Audio Effects",
                "Keyword Extractor; Voice Changer",
            )
        )
        self.assertFalse(
            tool_hint_covered_by_model(
                "Video-to-Audio",
                "Video Downloader -> Video-to-Audio",
            )
        )
        self.assertTrue(
            tool_hint_covered_by_model(
                "Audio-to-Text -> Article Spinner",
                "Audio-to-Text -> Text Paraphraser",
            )
        )
        self.assertFalse(
            tool_hint_covered_by_model(
                "Audio-to-Text -> Article Spinner",
                "Audio-to-Text -> Text Paraphraser -> URL Extractor",
            )
        )
        self.assertFalse(tool_hint_covered_by_model("", "Video Downloader"))

    def test_tool_hint_covered_by_model_uses_dataset_semantic_families(self) -> None:
        self.assertFalse(
            tool_hint_covered_by_model(
                "Audio-to-Text -> Article Spinner",
                "Audio-to-Text -> Text Paraphraser",
                semantic_tool_families={},
            )
        )
        self.assertTrue(
            tool_hint_covered_by_model(
                "Text Generation",
                "Summarization",
                semantic_tool_families={"hf_text_generation": ["Text Generation", "Summarization"]},
            )
        )

    def test_run_one_row_skips_planner_when_model_tool_matches_intent_hint(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Create audio from speech in the video.",
            "model tool": "Video Downloader -> Video-to-Audio",
            "intent tool": "Video Downloader, Video-to-Audio",
            "intent": "DownloadVideoFromURL -> ExtractAudioFromVideo",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Download.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                ],
                "task_links": [{"source": "Video Downloader", "target": "Video-to-Audio"}],
            }
        }
        args = argparse.Namespace(
            dry_run=False,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, [], args)

        self.assertEqual(result["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(result["result_source"], "previous_workflow")
        self.assertEqual(result["raw_planner_output"], "")
        self.assertIn("skip_replan", result["change_summary"])

    def test_run_one_row_skips_planner_when_coverage_failed(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Create audio from speech in the video.",
            "model tool": "Video-to-Audio",
            "intent tool": "",
            "intent": "",
            "coverage_warnings": "LLM_COVERAGE_CALL_FAILED: APITimeoutError: Request timed out.",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Search.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Video Search", "arguments": ["example video"]},
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                ],
                "task_links": [],
            }
        }
        args = argparse.Namespace(
            dry_run=False,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, [], args)

        self.assertEqual(result["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(result["result_source"], "previous_workflow")
        self.assertEqual(result["raw_planner_output"], "")
        self.assertIn("coverage_warnings present", result["change_summary"])
        self.assertIn("coverage_warning:", result["warnings"][0])

    def test_run_one_row_skips_planner_when_intent_hint_missing(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Create audio from speech in the video.",
            "model tool": "Video-to-Audio",
            "intent tool": "",
            "intent": "",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Search.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Video Search", "arguments": ["example video"]},
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                ],
                "task_links": [],
            }
        }
        args = argparse.Namespace(
            dry_run=False,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, [], args)

        self.assertEqual(result["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(result["result_source"], "previous_workflow")
        self.assertEqual(result["raw_planner_output"], "")
        self.assertIn("missing intent tool/intent hint", result["change_summary"])

    def test_run_one_row_skips_planner_when_previous_workflow_covers_hint(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Create audio from speech in the video.",
            "model tool": "Video-to-Audio",
            "intent tool": "Video Downloader -> Video-to-Audio",
            "intent": "DownloadVideoFromURL -> ExtractAudioFromVideo",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Download.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Video Downloader", "arguments": ["https://example.com/video.mp4"]},
                    {"task": "Video-to-Audio", "arguments": ["<node-0>"]},
                ],
                "task_links": [],
            }
        }
        args = argparse.Namespace(
            dry_run=False,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, [], args)

        self.assertEqual(result["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(result["result_source"], "previous_workflow")
        self.assertEqual(result["raw_planner_output"], "")
        self.assertIn("previous_workflow covers", result["change_summary"])

    def test_run_one_row_skips_planner_when_original_workflow_is_single(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Download a video and extract its audio.",
            "model tool": "Video-to-Audio",
            "intent tool": "Video Downloader -> Video-to-Audio",
            "intent": "DownloadVideoFromURL -> ExtractAudioFromVideo",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Extract audio."],
                "task_nodes": [{"task": "Video-to-Audio", "arguments": ["example.mp4"]}],
                "task_links": [],
            }
        }
        tool_desc = [
            {"tool_id": "Video Downloader", "intent": "DownloadVideoFromURL"},
            {"tool_id": "Video-to-Audio", "intent": "ExtractAudioFromVideo"},
        ]
        args = argparse.Namespace(
            dry_run=True,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, tool_desc, args)

        self.assertEqual(result["replan_decision"], "KEEP_ORIGINAL")
        self.assertEqual(result["result_source"], "previous_workflow")
        self.assertEqual(result["raw_planner_output"], "")
        self.assertIn("original_workflow_structure=single", result["change_summary"])

    def test_run_one_row_records_missing_hint_when_partially_covered(self) -> None:
        row = {
            "id": "case-1",
            "user_request": "Create audio from speech in the video.",
            "model tool": "Video-to-Audio",
            "intent tool": "Video Downloader -> Video-to-Audio",
            "intent": "DownloadVideoFromURL -> ExtractAudioFromVideo",
        }
        previous = {
            "result": {
                "task_steps": ["Step 1: Search.", "Step 2: Extract."],
                "task_nodes": [
                    {"task": "Text Search", "arguments": ["example video"]},
                    {"task": "Video-to-Audio", "arguments": ["example.mp4"]},
                ],
                "task_links": [],
            }
        }
        tool_desc = [
            {"tool_id": "Video Downloader", "intent": "DownloadVideoFromURL"},
            {"tool_id": "Video-to-Audio", "intent": "ExtractAudioFromVideo"},
        ]
        args = argparse.Namespace(
            dry_run=True,
            max_tools=80,
            planner_llm_config="missing-config.json",
            planner_llm_profile=None,
        )

        result = run_one_row(row, 1, {"case-1": previous}, tool_desc, args)

        self.assertEqual(result["replan_decision"], "DRY_RUN")
        self.assertEqual(result["covered_intent_tool_hint"], "Video-to-Audio")
        self.assertEqual(result["missing_intent_tool_hint"], "Video Downloader")
        self.assertEqual(result["covered_intent_hint"], "ExtractAudioFromVideo")
        self.assertEqual(result["missing_intent_hint"], "DownloadVideoFromURL")

    def test_call_failure_decision_is_not_empty(self) -> None:
        row = build_output_row(
            case_id="case-1",
            coverage_row={"user_request": "request"},
            previous_workflow={"task_steps": [], "task_nodes": [{"task": "Video-to-Audio"}], "task_links": []},
            planner_payload={},
            raw_planner_output="",
            warnings=["planner_call_error: APITimeoutError: Request timed out."],
        )

        self.assertEqual(row["replan_decision"], "PLANNER_CALL_FAILED")
        self.assertEqual(row["result_source"], "previous_workflow")

    def test_resume_results_exclude_planner_call_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_jsonl = Path(tmpdir) / "replan.jsonl"
            rows = [
                {"id": "ok-1", "replan_decision": "KEEP_ORIGINAL", "warnings": []},
                {
                    "id": "failed-1",
                    "replan_decision": "PLANNER_CALL_FAILED",
                    "warnings": ["planner_call_error: APIConnectionError: Connection error."],
                },
                {
                    "id": "legacy-failed-1",
                    "replan_decision": "QWEN_CALL_FAILED",
                    "warnings": ["qwen_call_error: APIConnectionError: Connection error."],
                },
            ]
            output_jsonl.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )

            resume_rows = load_resume_results(output_jsonl)

        self.assertEqual([row["id"] for row in resume_rows], ["ok-1"])
        self.assertFalse(is_retryable_replan_failure(resume_rows[0]))

    def test_latest_rows_by_id_keeps_last_record(self) -> None:
        rows = latest_rows_by_id(
            [
                {"id": "case-1", "replan_decision": "QWEN_CALL_FAILED"},
                {"id": "case-1", "replan_decision": "KEEP_ORIGINAL"},
                {"id": "case-2", "replan_decision": "KEEP_ORIGINAL"},
            ]
        )

        self.assertEqual(
            {row["id"]: row["replan_decision"] for row in rows},
            {"case-1": "KEEP_ORIGINAL", "case-2": "KEEP_ORIGINAL"},
        )

    def test_select_relevant_tools_uses_previous_workflow_and_intent_hints(self) -> None:
        tools = [
            {"tool_id": "Video Downloader", "intent": "DownloadVideoFromURL"},
            {"tool_id": "Video-to-Audio", "intent": "ExtractAudioFromVideo"},
            {"tool_id": "Image Search", "intent": "SearchImagesByQuery"},
        ]
        selected = select_relevant_tools(
            tool_desc=tools,
            previous_workflow={"task_nodes": [{"task": "Video-to-Audio"}]},
            previous_tool_summary="Video-to-Audio",
            intent_tool_hint="Video Downloader -> Video-to-Audio",
            intent_hint="DownloadVideoFromURL -> ExtractAudioFromVideo",
            max_tools=10,
        )

        self.assertEqual([tool["tool_id"] for tool in selected], ["Video Downloader", "Video-to-Audio"])


if __name__ == "__main__":
    unittest.main()




