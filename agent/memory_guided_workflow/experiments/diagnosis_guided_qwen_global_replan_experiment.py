# -*- coding: utf-8 -*-
"""Diagnosis-guided Qwen global workflow replanning experiment.

This is an offline experiment. It consumes GPT diagnosis rows and only sends
high-risk baseline workflows to Qwen for whole-workflow replanning. It can also
merge in a previous structural-repair output for structural LOCAL_REPAIR cases.

It does not modify the MIWP runtime, IncrementalPlanner, TaskUnderstanding, or
run_miwp_case.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.experiments import baseline_guided_workflow_critic_experiment as bg
from agent.memory_guided_workflow.experiments import baseline_guided_workflow_diagnosis_experiment as dg
from agent.memory_guided_workflow.experiments import diagnosis_guided_qwen_repair_experiment as qr
from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_JSONL = "agent/memory_guided_workflow/outputs/diagnosis_guided_qwen_global_replan.jsonl"
DEFAULT_OUTPUT_XLSX = "agent/memory_guided_workflow/outputs/diagnosis_guided_qwen_global_replan.xlsx"
SELECTION_MODES = (
    "semantic_risk",
    "global_only",
    "coverage_wrong_tool",
    "all_non_keep",
)
ALLOWED_DECISIONS = {"KEEP_ORIGINAL", "REPLAN"}
GLOBAL_ERROR_TYPES = {"coverage_missing", "wrong_decomposition", "wrong_tool"}
XLSX_HEADERS = [
    "id",
    "user_request",
    "selected_for_replan",
    "selection_mode",
    "selection_reason",
    "result_source",
    "replan_applied",
    "validation_status",
    "validation_reject_reason",
    "workflow_verdict",
    "confidence",
    "error_types",
    "structural_issue_types",
    "original_structural_error_count",
    "final_structural_error_count",
    "replan_decision",
    "required_operations",
    "baseline_failures",
    "replan_change_summary",
    "rejected_replan",
    "original_edges",
    "proposed_edges",
    "final_edges",
    "raw_qwen_replan_output",
    "warnings",
    "gpt_diagnosis",
    "structural_repair_row",
    "original_result",
    "qwen_proposed_result",
    "repaired_result",
]
_THREAD_LOCAL = threading.local()


def main() -> int:
    args = parse_args()
    diagnosis_rows = bg.read_jsonl_records(bg.resolve_path(args.diagnosis_jsonl))
    diagnosis_by_id = {str(row.get("id") or row.get("ID") or "").strip(): row for row in diagnosis_rows}
    structural_by_id = load_optional_rows(args.structural_repair_jsonl)

    if has_baseline_input(args):
        rows = bg.read_input_records(resolve_input_path(args), input_kind(args))
    else:
        rows = diagnosis_rows
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    output_jsonl = bg.resolve_path(args.output_jsonl)
    output_xlsx = bg.resolve_path(args.output_xlsx)
    completed_ids = bg.load_completed_ids(output_jsonl) if args.resume else set()
    results = bg.read_jsonl_records(output_jsonl) if args.resume and output_jsonl.exists() else []

    tool_catalog = bg.load_tool_catalog(bg.resolve_path(args.tool_desc))
    transition_index = bg.load_transition_index(args.transition_graph)

    pending: List[Tuple[int, Dict[str, Any], str, Dict[str, Any] | None, Dict[str, Any] | None]] = []
    for row_index, row in enumerate(rows, start=1):
        case_id = bg.get_case_id(row, row_index)
        if args.resume and case_id in completed_ids:
            print(f"[{row_index}/{len(rows)}] skip id={case_id} (resume)")
            continue
        pending.append((row_index, row, case_id, diagnosis_by_id.get(case_id), structural_by_id.get(case_id)))

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row_index, row, case_id, diagnosis, structural_row in pending:
            print(f"[{row_index}/{len(rows)}] replan id={case_id}")
            _, _, result = run_case_worker(
                row_index,
                row,
                case_id,
                diagnosis,
                structural_row,
                args,
                tool_catalog,
                transition_index,
            )
            bg.append_jsonl(output_jsonl, result)
            completed_ids.add(case_id)
            results.append(result)
    else:
        print(f"parallel_workers={workers}, pending_cases={len(pending)}")
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    run_case_worker,
                    row_index,
                    row,
                    case_id,
                    diagnosis,
                    structural_row,
                    args,
                    tool_catalog,
                    transition_index,
                ): (row_index, case_id)
                for row_index, row, case_id, diagnosis, structural_row in pending
            }
            for future in as_completed(futures):
                row_index, case_id = futures[future]
                try:
                    _, _, result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve row-level output.
                    result = error_result(case_id, "", None, None, None, [f"worker_error: {type(exc).__name__}: {exc}"])
                bg.append_jsonl(output_jsonl, result)
                completed_ids.add(case_id)
                results.append(result)
                print(f"[{row_index}/{len(rows)}] done id={case_id}")

    write_xlsx(output_xlsx, results)
    print(f"saved_jsonl={output_jsonl}")
    print(f"saved_xlsx={output_xlsx}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnosis-guided Qwen global workflow replanning experiment.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--input-json", default=None)
    group.add_argument("--input-jsonl", default=None)
    group.add_argument("--input-xlsx", default=None)
    parser.add_argument("--diagnosis-jsonl", required=True, help="GPT diagnosis JSONL output.")
    parser.add_argument("--structural-repair-jsonl", default=None, help="Optional structural repair JSONL to merge.")
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--transition-graph", default=None)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--qwen-llm-config", default="configs/qwen.json")
    parser.add_argument("--qwen-llm-profile", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--selection-mode", choices=SELECTION_MODES, default="semantic_risk")
    parser.add_argument("--max-tool-knowledge", type=int, default=80)
    parser.add_argument("--max-node-count", type=int, default=10)
    parser.add_argument("--max-node-delta", type=int, default=4)
    parser.add_argument(
        "--allow-partial-structural-repair",
        action="store_true",
        help="Merge structural repair rows even when final_structural_error_count is not zero.",
    )
    parser.add_argument("--disable-literal-preservation-gate", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Do not call Qwen; only test selection/merge plumbing.")
    return parser.parse_args()


def has_baseline_input(args: argparse.Namespace) -> bool:
    return bool(args.input_json or args.input_jsonl or args.input_xlsx)


def input_kind(args: argparse.Namespace) -> str:
    if args.input_json:
        return "json"
    if args.input_jsonl:
        return "jsonl"
    return "xlsx"


def resolve_input_path(args: argparse.Namespace) -> Path:
    return bg.resolve_path(args.input_json or args.input_jsonl or args.input_xlsx)


def load_optional_rows(raw_path: Any) -> Dict[str, Dict[str, Any]]:
    if not raw_path:
        return {}
    rows = bg.read_jsonl_records(bg.resolve_path(raw_path))
    return {str(row.get("id") or row.get("ID") or "").strip(): row for row in rows}


def run_case_worker(
    row_index: int,
    row: Dict[str, Any],
    case_id: str,
    diagnosis: Dict[str, Any] | None,
    structural_row: Dict[str, Any] | None,
    args: argparse.Namespace,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> Tuple[int, str, Dict[str, Any]]:
    try:
        client = None if args.dry_run else get_thread_client(args.qwen_llm_config, args.qwen_llm_profile)
        result = run_one_case(row, case_id, diagnosis, structural_row, client, args, tool_catalog, transition_index)
    except Exception as exc:  # noqa: BLE001 - keep row-level output.
        user_request = bg.get_first_value(row, ["user_request", "request", "instruction", "User Request"])
        result = error_result(case_id, user_request, None, diagnosis, structural_row, [f"case_error: {type(exc).__name__}: {exc}"])
    return row_index, case_id, result


def get_thread_client(llm_config: Any, llm_profile: str | None) -> OpenAICompatibleLLMClient:
    signature = (str(llm_config or ""), str(llm_profile or ""))
    if getattr(_THREAD_LOCAL, "client_signature", None) != signature:
        _THREAD_LOCAL.client = OpenAICompatibleLLMClient(llm_config_path=llm_config, llm_profile=llm_profile)
        _THREAD_LOCAL.client_signature = signature
    return _THREAD_LOCAL.client


def run_one_case(
    row: Dict[str, Any],
    case_id: str,
    diagnosis: Dict[str, Any] | None,
    structural_row: Dict[str, Any] | None,
    client: OpenAICompatibleLLMClient | None,
    args: argparse.Namespace,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> Dict[str, Any]:
    warnings: List[str] = []
    user_request = (
        bg.get_first_value(row, ["user_request", "request", "instruction", "User Request"])
        or bg.get_first_value(diagnosis or {}, ["user_request", "request", "instruction", "User Request"])
    )
    original_result = qr.extract_original_result(row, diagnosis, warnings)
    if original_result is None:
        return error_result(case_id, user_request, None, diagnosis, structural_row, warnings + ["missing workflow result"])

    original_working = copy.deepcopy(original_result)
    original_nodes = bg.normalize_task_nodes(original_working, warnings)
    original_edges = bg.normalize_workflow_edges(original_working, original_nodes, warnings)
    diagnostics = qr.normalize_or_build_diagnostics(diagnosis, original_nodes, original_edges, tool_catalog)
    node_context = bg.build_node_context(original_nodes, tool_catalog)
    candidate_evidence = qr.normalize_candidate_evidence(
        diagnosis,
        original_nodes,
        original_edges,
        diagnostics,
        tool_catalog,
        transition_index,
    )
    gpt_suggestion = qr.build_gpt_repair_suggestion(diagnosis, diagnostics, candidate_evidence)
    selected, selection_reason = should_replan(gpt_suggestion, diagnostics, args.selection_mode)

    structural_result, structural_source = choose_structural_result(
        structural_row,
        allow_partial=args.allow_partial_structural_repair,
    )
    baseline_result = structural_result if structural_result is not None and not selected else original_result
    result_source = structural_source if structural_result is not None and not selected else "original"
    baseline_edges = original_edges
    if baseline_result is not original_result:
        baseline_warnings: List[str] = []
        baseline_working = bg.ensure_result_shape(copy.deepcopy(baseline_result), baseline_warnings)
        baseline_nodes = bg.normalize_task_nodes(baseline_working, baseline_warnings)
        baseline_edges = bg.normalize_workflow_edges(baseline_working, baseline_nodes, baseline_warnings)
        warnings.extend(qr.prefix_warnings("structural_repair", baseline_warnings))

    base = base_output(
        case_id=case_id,
        user_request=user_request,
        original_result=original_result,
        repaired_result=baseline_result,
        result_source=result_source,
        diagnosis=diagnosis,
        structural_row=structural_row,
        gpt_suggestion=gpt_suggestion,
        diagnostics=diagnostics,
        original_edges=original_edges,
        final_edges=baseline_edges,
        selected=selected,
        selection_mode=args.selection_mode,
        selection_reason=selection_reason,
        warnings=warnings,
    )
    if not selected:
        return base
    if args.dry_run:
        base["validation_status"] = "dry_run"
        base["warnings"] = warnings + ["dry_run: selected case was not sent to Qwen"]
        return base
    if client is None:
        base["validation_status"] = "qwen_skipped"
        base["warnings"] = warnings + ["missing qwen client"]
        return base

    prompt = build_replan_prompt(
        user_request=user_request,
        baseline_workflow=original_result,
        original_nodes=original_nodes,
        original_edges=original_edges,
        node_context=node_context,
        gpt_suggestion=gpt_suggestion,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        max_tool_knowledge=args.max_tool_knowledge,
        max_node_count=args.max_node_count,
        max_node_delta=args.max_node_delta,
    )
    raw_qwen, payload = call_json_replanner(client, prompt, warnings)
    replan_payload = normalize_replan_payload(payload, warnings)
    base["raw_qwen_replan_output"] = raw_qwen
    base["replan_decision"] = replan_payload["replan_decision"]
    base["required_operations"] = replan_payload["required_operations"]
    base["baseline_failures"] = replan_payload["baseline_failures"]

    if replan_payload["replan_decision"] != "REPLAN":
        base["validation_status"] = "qwen_keep_original"
        base["result_source"] = result_source
        base["warnings"] = warnings
        return base

    validation = validate_replan(
        original_result=original_result,
        original_nodes=original_nodes,
        original_edges=original_edges,
        proposed_result=replan_payload["replanned_workflow"],
        tool_catalog=tool_catalog,
        user_request=user_request,
        max_node_count=max(1, int(args.max_node_count or 1)),
        max_node_delta=max(0, int(args.max_node_delta or 0)),
        require_literal_preservation=not args.disable_literal_preservation_gate,
        warnings=warnings,
    )
    base["qwen_proposed_result"] = validation.get("qwen_proposed_result")
    base["proposed_edges"] = validation.get("proposed_edges", [])
    base["replan_change_summary"] = validation.get("change_summary", {})
    base["validation_status"] = validation["status"]
    base["validation_reject_reason"] = validation["reject_reason"]
    base["rejected_replan"] = validation.get("rejected_replan", {})
    base["final_structural_error_count"] = validation.get("final_structural_error_count", diagnostics.get("structural_error_count", 0))
    if validation["accepted"]:
        base["replan_applied"] = True
        base["result_source"] = "global_replan"
        base["repaired_result"] = validation["repaired_result"]
        base["result"] = validation["repaired_result"]
        base["final_edges"] = validation["final_edges"]
    base["warnings"] = warnings
    return base


def should_replan(
    suggestion: Dict[str, Any],
    diagnostics: Dict[str, Any],
    selection_mode: str,
) -> Tuple[bool, str]:
    verdict = str(suggestion.get("workflow_verdict") or "KEEP").upper()
    errors = set(str(item) for item in bg.coerce_list(suggestion.get("error_types", [])))
    structural_issues = set(str(item) for item in bg.coerce_list(diagnostics.get("issue_types", [])))
    has_global_error = bool(errors & GLOBAL_ERROR_TYPES)
    if selection_mode == "global_only":
        return verdict == "GLOBAL_REPLAN", "GLOBAL_REPLAN selected" if verdict == "GLOBAL_REPLAN" else "not GLOBAL_REPLAN"
    if selection_mode == "coverage_wrong_tool":
        selected = verdict != "KEEP" and has_global_error
        return selected, "non-KEEP with coverage/decomposition/tool error" if selected else "not coverage/decomposition/tool risk"
    if selection_mode == "all_non_keep":
        return verdict != "KEEP", "non-KEEP selected" if verdict != "KEEP" else "KEEP skipped"
    if selection_mode == "semantic_risk":
        if verdict == "GLOBAL_REPLAN":
            return True, "GLOBAL_REPLAN selected"
        if verdict == "LOCAL_REPAIR" and not structural_issues and has_global_error:
            return True, "semantic LOCAL_REPAIR with coverage/decomposition/tool error"
        return False, "not semantic global-replan risk"
    return False, f"unknown selection_mode={selection_mode}"


def choose_structural_result(
    structural_row: Dict[str, Any] | None,
    allow_partial: bool,
) -> Tuple[Dict[str, Any] | None, str]:
    if not isinstance(structural_row, dict):
        return None, ""
    if not structural_row.get("qwen_repair_applied"):
        return None, ""
    if not allow_partial and int(structural_row.get("final_structural_error_count") or 0) != 0:
        return None, ""
    result = bg.parse_jsonish(structural_row.get("repaired_result") or structural_row.get("result"))
    if isinstance(result, dict) and "task_nodes" in result:
        return result, "structural_repair"
    return None, ""


def base_output(
    case_id: str,
    user_request: str,
    original_result: Dict[str, Any],
    repaired_result: Dict[str, Any],
    result_source: str,
    diagnosis: Dict[str, Any] | None,
    structural_row: Dict[str, Any] | None,
    gpt_suggestion: Dict[str, Any],
    diagnostics: Dict[str, Any],
    original_edges: List[Dict[str, int]],
    final_edges: List[Dict[str, int]],
    selected: bool,
    selection_mode: str,
    selection_reason: str,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": repaired_result,
        "result": repaired_result,
        "selected_for_replan": selected,
        "selection_mode": selection_mode,
        "selection_reason": selection_reason,
        "result_source": result_source,
        "replan_applied": False,
        "validation_status": "not_selected" if not selected else "pending",
        "validation_reject_reason": "",
        "workflow_verdict": gpt_suggestion.get("workflow_verdict", "KEEP"),
        "confidence": gpt_suggestion.get("confidence", "low"),
        "error_types": gpt_suggestion.get("error_types", []),
        "structural_issue_types": diagnostics.get("issue_types", []),
        "original_structural_error_count": diagnostics.get("structural_error_count", 0),
        "final_structural_error_count": diagnostics.get("structural_error_count", 0),
        "replan_decision": "",
        "required_operations": [],
        "baseline_failures": [],
        "replan_change_summary": {},
        "rejected_replan": {},
        "original_edges": original_edges,
        "proposed_edges": [],
        "final_edges": final_edges,
        "raw_qwen_replan_output": "",
        "warnings": warnings,
        "gpt_diagnosis": diagnosis or {},
        "structural_repair_row": structural_row or {},
        "qwen_proposed_result": None,
    }


def build_replan_prompt(
    user_request: str,
    baseline_workflow: Dict[str, Any],
    original_nodes: List[Dict[str, Any]],
    original_edges: List[Dict[str, int]],
    node_context: List[Dict[str, Any]],
    gpt_suggestion: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    max_tool_knowledge: int,
    max_node_count: int,
    max_node_delta: int,
) -> str:
    payload = {
        "user_request": user_request,
        "baseline_workflow": baseline_workflow,
        "baseline_nodes_with_tool_knowledge": node_context,
        "normalized_edges": bg.enrich_edges(original_edges, original_nodes, tool_catalog, transition_index),
        "gpt_critic_suggestion": gpt_suggestion,
        "available_tools": dg.compact_tool_knowledge(tool_catalog, max_tool_knowledge),
        "replan_limits": {
            "max_node_count": max(1, int(max_node_count or 1)),
            "max_node_delta_from_baseline": max(0, int(max_node_delta or 0)),
            "baseline_node_count": len(original_nodes),
            "return_original_if_uncertain": True,
        },
    }
    return f"""You are a Baseline-Guided Whole Workflow Replanner.

You are given:
- a user request;
- a baseline workflow generated by Qwen;
- a GPT critic diagnosis;
- tool descriptions and type information.

Your job is to decide whether the baseline workflow is semantically wrong enough
to require whole-workflow replanning.

Default action:
Return KEEP_ORIGINAL and copy the baseline workflow.

Only return REPLAN when the GPT critic identifies a major request coverage,
task decomposition, or wrong-tool problem and you can produce a clearly better
complete workflow.

Mandatory reasoning steps:
1. Extract the required operations from the user request.
2. Compare each required operation against the baseline workflow.
3. If the baseline is only type-compatible but semantically misses the request,
   generate a complete corrected workflow.
4. If uncertain, return KEEP_ORIGINAL.

Hard constraints:
- Use only tools from available_tools.
- Preserve user-provided filenames, URLs, quoted text, effect names, speed values,
  voice/style parameters, and other literal inputs needed by the request.
- task_nodes must be in executable order.
- Node references must use <node-i> where i is an earlier node index.
- task_links must be consistent with <node-i> arguments.
- Do not exceed replan_limits.max_node_count.
- Do not add unrelated optional steps.
- Do not use gold labels.

Return JSON only:
{{
  "replan_decision": "KEEP_ORIGINAL | REPLAN",
  "required_operations": [
    {{"operation": "...", "required_artifact": "...", "reason": "..."}}
  ],
  "baseline_failures": [
    {{"kind": "missing_operation | wrong_tool | wrong_order | wrong_modality | wrong_dependency", "nodes": [0], "reason": "..."}}
  ],
  "replanned_workflow": {{
    "task_steps": ["Step 1: ..."],
    "task_nodes": [
      {{"task": "...", "arguments": ["...", "<node-0>"]}}
    ],
    "task_links": [
      {{"source": "...", "target": "..."}}
    ]
  }},
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def call_json_replanner(
    client: OpenAICompatibleLLMClient,
    prompt: str,
    warnings: List[str],
) -> Tuple[str, Dict[str, Any]]:
    raw_text = ""
    try:
        raw_text = client.chat(
            messages=[
                {"role": "system", "content": "You are a conservative baseline-guided workflow replanner. Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        return raw_text, extract_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001 - row-level failure should not stop the run.
        warnings.append(f"qwen_replan_error: {type(exc).__name__}: {exc}")
        return raw_text, {}


def normalize_replan_payload(payload: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        warnings.append("qwen replan payload is not an object; keep original")
        return default_replan_payload()
    decision = str(payload.get("replan_decision") or "").strip().upper()
    if decision not in ALLOWED_DECISIONS:
        warnings.append(f"invalid replan_decision={decision!r}; keep original")
        decision = "KEEP_ORIGINAL"
    workflow = bg.parse_jsonish(payload.get("replanned_workflow"))
    if not isinstance(workflow, dict) and "task_nodes" in payload:
        workflow = payload
    return {
        "replan_decision": decision,
        "required_operations": normalize_object_list(payload.get("required_operations")),
        "baseline_failures": normalize_object_list(payload.get("baseline_failures")),
        "replanned_workflow": workflow if isinstance(workflow, dict) else None,
        "reason": str(payload.get("reason") or ""),
    }


def default_replan_payload() -> Dict[str, Any]:
    return {
        "replan_decision": "KEEP_ORIGINAL",
        "required_operations": [],
        "baseline_failures": [],
        "replanned_workflow": None,
        "reason": "",
    }


def normalize_object_list(value: Any) -> List[Dict[str, Any]]:
    result = []
    for item in bg.coerce_list(bg.parse_jsonish(value)):
        if isinstance(item, dict):
            result.append(item)
    return result


def validate_replan(
    original_result: Dict[str, Any],
    original_nodes: List[Dict[str, Any]],
    original_edges: List[Dict[str, int]],
    proposed_result: Dict[str, Any] | None,
    tool_catalog: Dict[str, Dict[str, Any]],
    user_request: str,
    max_node_count: int,
    max_node_delta: int,
    require_literal_preservation: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    if not isinstance(proposed_result, dict):
        return reject_validation("missing replanned_workflow", proposed_result, [], 0)
    local_warnings: List[str] = []
    proposed_working = bg.ensure_result_shape(copy.deepcopy(proposed_result), local_warnings)
    proposed_nodes = bg.normalize_task_nodes(proposed_working, local_warnings)
    if not proposed_nodes:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation("proposed task_nodes is empty", proposed_working, [], 0)
    if len(proposed_nodes) > max_node_count:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation(f"too many nodes: {len(proposed_nodes)} > {max_node_count}", proposed_working, [], 0)
    if abs(len(proposed_nodes) - len(original_nodes)) > max_node_delta:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation(
            f"node delta too large: {abs(len(proposed_nodes) - len(original_nodes))} > {max_node_delta}",
            proposed_working,
            [],
            0,
        )

    tool_reason = validate_all_tools_known(proposed_nodes, tool_catalog)
    if tool_reason:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation(tool_reason, proposed_working, [], 0)

    proposed_edges = bg.normalize_workflow_edges(proposed_working, proposed_nodes, local_warnings)
    edge_reason = qr.validate_edges(proposed_nodes, proposed_edges, tool_catalog)
    if edge_reason:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation(edge_reason, proposed_working, proposed_edges, 0)

    diagnostics = dg.build_structural_diagnostics(proposed_nodes, proposed_edges, tool_catalog)
    final_error_count = int(diagnostics.get("structural_error_count", 0) or 0)
    if final_error_count:
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation(
            f"replanned workflow still has structural errors: {final_error_count}",
            proposed_working,
            proposed_edges,
            final_error_count,
        )

    if require_literal_preservation:
        missing_literals = missing_required_literals(original_result, proposed_working, user_request)
        if missing_literals:
            warnings.extend(qr.prefix_warnings("proposed", local_warnings))
            return reject_validation(
                f"missing required literals: {missing_literals[:5]}",
                proposed_working,
                proposed_edges,
                final_error_count,
            )

    repaired_result = bg.rebuild_result(proposed_working, proposed_nodes, proposed_edges, [], user_request)
    change_summary = qr.build_change_summary(original_result, repaired_result, original_edges, proposed_edges)
    if not change_summary.get("has_change"):
        warnings.extend(qr.prefix_warnings("proposed", local_warnings))
        return reject_validation("replan has no deterministic change", proposed_working, proposed_edges, final_error_count)

    warnings.extend(qr.prefix_warnings("proposed", local_warnings))
    return {
        "accepted": True,
        "status": "accepted",
        "reject_reason": "",
        "repaired_result": repaired_result,
        "qwen_proposed_result": proposed_working,
        "proposed_edges": proposed_edges,
        "final_edges": proposed_edges,
        "change_summary": change_summary,
        "final_structural_error_count": final_error_count,
        "rejected_replan": {},
    }


def validate_all_tools_known(nodes: List[Dict[str, Any]], tool_catalog: Dict[str, Dict[str, Any]]) -> str:
    for index, node in enumerate(nodes):
        task = node.get("task", "")
        if not bg.tool_is_known(bg.lookup_tool(tool_catalog, task)):
            return f"unknown proposed tool at node {index}: {task}"
    return ""


def missing_required_literals(
    original_result: Dict[str, Any],
    proposed_result: Dict[str, Any],
    user_request: str,
) -> List[str]:
    required = extract_literal_anchors(original_result, user_request)
    proposed_text = json.dumps(proposed_result, ensure_ascii=False)
    missing = []
    for literal in required:
        if literal and literal not in proposed_text:
            missing.append(literal)
    return missing


def extract_literal_anchors(original_result: Dict[str, Any], user_request: str) -> List[str]:
    request_text = str(user_request or "")
    anchors: List[str] = []
    patterns = [
        r"[\w./\\:-]+\.(?:jpg|jpeg|png|gif|bmp|tiff|svg|ico|mp3|wav|wma|ogg|aac|flac|aiff|au|mp4|avi|mov|flv|wmv|mkv|webm|m4v|mpg|mpeg|txt|pdf|docx?)",
        r"https?://[^\s'\"<>]+",
        r"www\.[^\s'\"<>]+",
    ]
    for pattern in patterns:
        anchors.extend(match.group(0).strip(".,;:") for match in re.finditer(pattern, request_text, flags=re.IGNORECASE))
    for match in re.finditer(r"'([^']{1,120})'|\"([^\"]{1,120})\"", request_text):
        value = (match.group(1) or match.group(2) or "").strip()
        if value and should_preserve_literal(value):
            anchors.append(value)
    for node in bg.coerce_list(original_result.get("task_nodes", [])):
        if not isinstance(node, dict):
            continue
        for argument in bg.coerce_list(node.get("arguments", [])):
            if bg.parse_node_ref(argument) is not None:
                continue
            text = str(argument or "").strip()
            if should_preserve_literal(text) and (text in request_text or looks_like_file_url_or_parameter(text)):
                anchors.append(text)
    return bg.dedupe_preserve_order(anchors)


def should_preserve_literal(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if looks_like_file_url_or_parameter(text):
        return True
    if len(text) <= 80 and not text.lower().startswith(("step ", "node ")):
        return True
    return False


def looks_like_file_url_or_parameter(value: str) -> bool:
    text = str(value or "").strip()
    if re.search(r"\.(?:jpg|jpeg|png|gif|bmp|tiff|svg|ico|mp3|wav|wma|ogg|aac|flac|aiff|au|mp4|avi|mov|flv|wmv|mkv|webm|m4v|mpg|mpeg|txt|pdf|docx?)\b", text, re.IGNORECASE):
        return True
    if re.search(r"^(?:https?://|www\.)", text, re.IGNORECASE):
        return True
    if re.search(r"\b(?:reverb|chorus|equalization|female voice|male voice|speed|tone|pitch|style)\b", text, re.IGNORECASE):
        return True
    return False


def reject_validation(
    reason: str,
    proposed_result: Any,
    proposed_edges: List[Dict[str, int]],
    final_structural_error_count: int,
) -> Dict[str, Any]:
    return {
        "accepted": False,
        "status": "rejected",
        "reject_reason": reason,
        "qwen_proposed_result": proposed_result,
        "proposed_edges": proposed_edges,
        "final_edges": [],
        "change_summary": {},
        "final_structural_error_count": final_structural_error_count,
        "rejected_replan": {"reason": reason, "proposed_result": proposed_result, "proposed_edges": proposed_edges},
    }


def error_result(
    case_id: str,
    user_request: str,
    original_result: Any,
    diagnosis: Dict[str, Any] | None,
    structural_row: Dict[str, Any] | None,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": original_result,
        "result": original_result,
        "selected_for_replan": False,
        "selection_mode": "",
        "selection_reason": "",
        "result_source": "error",
        "replan_applied": False,
        "validation_status": "error",
        "validation_reject_reason": "",
        "workflow_verdict": "KEEP",
        "confidence": "low",
        "error_types": [],
        "structural_issue_types": [],
        "original_structural_error_count": 0,
        "final_structural_error_count": 0,
        "replan_decision": "",
        "required_operations": [],
        "baseline_failures": [],
        "replan_change_summary": {},
        "rejected_replan": {},
        "original_edges": [],
        "proposed_edges": [],
        "final_edges": [],
        "raw_qwen_replan_output": "",
        "warnings": warnings,
        "gpt_diagnosis": diagnosis or {},
        "structural_repair_row": structural_row or {},
        "qwen_proposed_result": None,
    }


def write_xlsx(path: Path, rows: List[Dict[str, Any]]) -> None:
    xlsx_rows: List[List[Any]] = [XLSX_HEADERS]
    for row in rows:
        xlsx_rows.append([xlsx_value(row.get(header, "")) for header in XLSX_HEADERS])
    bg.write_xlsx_rows(path, xlsx_rows)


def xlsx_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


if __name__ == "__main__":
    raise SystemExit(main())
