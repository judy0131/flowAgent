# -*- coding: utf-8 -*-
"""Diagnosis-guided Qwen workflow repair experiment.

This is an offline experiment:
1. Read a baseline workflow and a GPT diagnosis row.
2. Pass the GPT diagnosis/suggestion to Qwen.
3. Let Qwen propose a repaired workflow.
4. Apply a conservative deterministic validator; fallback to the baseline on failure.

It does not modify the MIWP runtime, IncrementalPlanner, TaskUnderstanding, or
run_miwp_case.py.
"""

from __future__ import annotations

import argparse
import copy
import json
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
from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_JSONL = "agent/memory_guided_workflow/outputs/diagnosis_guided_qwen_repair.jsonl"
DEFAULT_OUTPUT_XLSX = "agent/memory_guided_workflow/outputs/diagnosis_guided_qwen_repair.xlsx"
SELECTION_MODES = ("structural_only", "local_only", "all_non_keep", "global_only")
ALLOWED_DECISIONS = {"KEEP_ORIGINAL", "REPAIR"}
XLSX_HEADERS = [
    "id",
    "user_request",
    "selected_for_repair",
    "selection_mode",
    "selection_reason",
    "qwen_repair_applied",
    "validation_status",
    "validation_reject_reason",
    "workflow_verdict",
    "confidence",
    "error_types",
    "structural_issue_types",
    "original_structural_error_count",
    "final_structural_error_count",
    "repair_scope",
    "qwen_repair_decision",
    "qwen_repair_operations",
    "deterministic_change_summary",
    "rejected_repair",
    "original_edges",
    "proposed_edges",
    "final_edges",
    "raw_qwen_repair_output",
    "warnings",
    "gpt_diagnosis",
    "original_result",
    "qwen_proposed_result",
    "repaired_result",
]
_THREAD_LOCAL = threading.local()


def main() -> int:
    args = parse_args()
    diagnosis_rows = bg.read_jsonl_records(bg.resolve_path(args.diagnosis_jsonl))
    diagnosis_by_id = {str(row.get("id") or row.get("ID") or "").strip(): row for row in diagnosis_rows}

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

    pending: List[Tuple[int, Dict[str, Any], str, Dict[str, Any] | None]] = []
    for row_index, row in enumerate(rows, start=1):
        case_id = bg.get_case_id(row, row_index)
        diagnosis = diagnosis_by_id.get(case_id)
        if args.resume and case_id in completed_ids:
            print(f"[{row_index}/{len(rows)}] skip id={case_id} (resume)")
            continue
        pending.append((row_index, row, case_id, diagnosis))

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row_index, row, case_id, diagnosis in pending:
            print(f"[{row_index}/{len(rows)}] repair id={case_id}")
            _, _, result = run_case_worker(
                row_index=row_index,
                row=row,
                case_id=case_id,
                diagnosis=diagnosis,
                args=args,
                tool_catalog=tool_catalog,
                transition_index=transition_index,
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
                    args,
                    tool_catalog,
                    transition_index,
                ): (row_index, case_id)
                for row_index, row, case_id, diagnosis in pending
            }
            for future in as_completed(futures):
                row_index, case_id = futures[future]
                try:
                    _, _, result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve row-level output.
                    result = error_result(case_id, "", None, None, [f"worker_error: {type(exc).__name__}: {exc}"])
                bg.append_jsonl(output_jsonl, result)
                completed_ids.add(case_id)
                results.append(result)
                print(f"[{row_index}/{len(rows)}] done id={case_id}")

    write_xlsx(output_xlsx, results)
    print(f"saved_jsonl={output_jsonl}")
    print(f"saved_xlsx={output_xlsx}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnosis-guided Qwen workflow repair experiment.")
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--input-json", default=None)
    group.add_argument("--input-jsonl", default=None)
    group.add_argument("--input-xlsx", default=None)
    parser.add_argument("--diagnosis-jsonl", required=True, help="GPT diagnosis JSONL output.")
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--transition-graph", default=None)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--qwen-llm-config", default="configs/qwen.json")
    parser.add_argument("--qwen-llm-profile", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--selection-mode", choices=SELECTION_MODES, default="structural_only")
    parser.add_argument("--max-tool-knowledge", type=int, default=80)
    parser.add_argument("--max-new-nodes", type=int, default=1)
    parser.add_argument("--allow-global-replan", action="store_true")
    parser.add_argument(
        "--no-require-structural-improvement",
        action="store_true",
        help="Do not require structural_error_count to decrease for structural repairs.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not call Qwen; only test selection/validation plumbing.")
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


def run_case_worker(
    row_index: int,
    row: Dict[str, Any],
    case_id: str,
    diagnosis: Dict[str, Any] | None,
    args: argparse.Namespace,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> Tuple[int, str, Dict[str, Any]]:
    try:
        client = None if args.dry_run else get_thread_client(args.qwen_llm_config, args.qwen_llm_profile)
        result = run_one_case(row, case_id, diagnosis, client, args, tool_catalog, transition_index)
    except Exception as exc:  # noqa: BLE001 - keep row-level output.
        user_request = bg.get_first_value(row, ["user_request", "request", "instruction", "User Request"])
        result = error_result(case_id, user_request, None, diagnosis, [f"case_error: {type(exc).__name__}: {exc}"])
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
    original_result = extract_original_result(row, diagnosis, warnings)
    if original_result is None:
        return error_result(case_id, user_request, None, diagnosis, warnings + ["missing workflow result"])

    original_working = copy.deepcopy(original_result)
    original_nodes = bg.normalize_task_nodes(original_working, warnings)
    original_edges = bg.normalize_workflow_edges(original_working, original_nodes, warnings)
    diagnostics = normalize_or_build_diagnostics(diagnosis, original_nodes, original_edges, tool_catalog)
    node_context = bg.build_node_context(original_nodes, tool_catalog)
    candidate_evidence = normalize_candidate_evidence(
        diagnosis,
        original_nodes,
        original_edges,
        diagnostics,
        tool_catalog,
        transition_index,
    )
    gpt_suggestion = build_gpt_repair_suggestion(diagnosis, diagnostics, candidate_evidence)
    selected, selection_reason = should_repair(gpt_suggestion, diagnostics, args.selection_mode, args.allow_global_replan)

    base_result = base_output(
        case_id=case_id,
        user_request=user_request,
        original_result=original_result,
        diagnosis=diagnosis,
        gpt_suggestion=gpt_suggestion,
        diagnostics=diagnostics,
        original_edges=original_edges,
        selection_mode=args.selection_mode,
        selected=selected,
        selection_reason=selection_reason,
        warnings=warnings,
    )
    if not selected:
        return base_result
    if args.dry_run:
        base_result["warnings"] = warnings + ["dry_run: selected case was not sent to Qwen"]
        base_result["validation_status"] = "dry_run"
        return base_result
    if client is None:
        base_result["warnings"] = warnings + ["missing qwen client"]
        base_result["validation_status"] = "qwen_skipped"
        return base_result

    prompt = build_qwen_repair_prompt(
        user_request=user_request,
        baseline_workflow=original_result,
        original_nodes=original_nodes,
        original_edges=original_edges,
        node_context=node_context,
        gpt_suggestion=gpt_suggestion,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        max_tool_knowledge=args.max_tool_knowledge,
        max_new_nodes=args.max_new_nodes,
    )
    raw_qwen, payload = call_json_repair(client, prompt, warnings)
    repair_payload = normalize_qwen_repair_payload(payload, warnings)
    base_result["raw_qwen_repair_output"] = raw_qwen
    base_result["qwen_repair_decision"] = repair_payload["repair_decision"]
    base_result["qwen_repair_operations"] = repair_payload["repair_operations"]

    if repair_payload["repair_decision"] != "REPAIR":
        base_result["validation_status"] = "qwen_keep_original"
        base_result["warnings"] = warnings
        return base_result

    validation = validate_qwen_repair(
        original_result=original_result,
        original_nodes=original_nodes,
        original_edges=original_edges,
        proposed_result=repair_payload["repaired_workflow"],
        diagnostics=diagnostics,
        gpt_suggestion=gpt_suggestion,
        tool_catalog=tool_catalog,
        user_request=user_request,
        max_new_nodes=max(0, int(args.max_new_nodes or 0)),
        require_structural_improvement=not args.no_require_structural_improvement,
        warnings=warnings,
    )
    base_result["qwen_proposed_result"] = validation.get("qwen_proposed_result")
    base_result["proposed_edges"] = validation.get("proposed_edges", [])
    base_result["deterministic_change_summary"] = validation.get("change_summary", {})
    base_result["validation_status"] = validation["status"]
    base_result["validation_reject_reason"] = validation["reject_reason"]
    base_result["rejected_repair"] = validation.get("rejected_repair", {})
    base_result["final_structural_error_count"] = validation.get(
        "final_structural_error_count",
        diagnostics.get("structural_error_count", 0),
    )
    if validation["accepted"]:
        base_result["qwen_repair_applied"] = True
        base_result["repaired_result"] = validation["repaired_result"]
        base_result["result"] = validation["repaired_result"]
        base_result["final_edges"] = validation["final_edges"]
    base_result["warnings"] = warnings
    return base_result


def extract_original_result(
    row: Dict[str, Any],
    diagnosis: Dict[str, Any] | None,
    warnings: List[str],
) -> Dict[str, Any] | None:
    for source in (row, diagnosis or {}):
        payload = bg.parse_jsonish(source.get("original_result")) if isinstance(source, dict) else None
        if isinstance(payload, dict) and "task_nodes" in payload:
            return bg.ensure_result_shape(payload, warnings)
    return bg.extract_baseline_result(row, warnings)


def normalize_or_build_diagnostics(
    diagnosis: Dict[str, Any] | None,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    raw = bg.parse_jsonish((diagnosis or {}).get("structural_diagnostics"))
    if isinstance(raw, dict):
        raw.setdefault("issue_types", bg.coerce_list(raw.get("issue_types", [])))
        raw.setdefault("type_incompatible_edges", bg.coerce_list(raw.get("type_incompatible_edges", [])))
        raw.setdefault("missing_required_slots", bg.coerce_list(raw.get("missing_required_slots", [])))
        raw.setdefault("invalid_node_refs", bg.coerce_list(raw.get("invalid_node_refs", [])))
        raw.setdefault("unknown_tools", bg.coerce_list(raw.get("unknown_tools", [])))
        raw["structural_error_count"] = int(
            raw.get("structural_error_count")
            or (
                len(raw.get("type_incompatible_edges", []))
                + len(raw.get("missing_required_slots", []))
                + len(raw.get("invalid_node_refs", []))
                + len(raw.get("unknown_tools", []))
            )
        )
        return raw
    return dg.build_structural_diagnostics(nodes, edges, tool_catalog)


def normalize_candidate_evidence(
    diagnosis: Dict[str, Any] | None,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    diagnostics: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> Dict[str, Any]:
    raw = bg.parse_jsonish((diagnosis or {}).get("candidate_repair_evidence"))
    if isinstance(raw, dict):
        return raw
    return dg.build_candidate_repair_evidence(nodes, edges, diagnostics, tool_catalog, transition_index)


def build_gpt_repair_suggestion(
    diagnosis: Dict[str, Any] | None,
    diagnostics: Dict[str, Any],
    candidate_evidence: Dict[str, Any],
) -> Dict[str, Any]:
    diagnosis = diagnosis or {}
    repair_scope = bg.parse_jsonish(diagnosis.get("repair_scope"))
    if not isinstance(repair_scope, dict):
        repair_scope = dg.default_repair_scope()
    suggestion = {
        "workflow_verdict": str(diagnosis.get("workflow_verdict") or "KEEP").strip().upper(),
        "confidence": str(diagnosis.get("confidence") or "low").strip().lower(),
        "error_types": bg.coerce_list(bg.parse_jsonish(diagnosis.get("error_types"))),
        "coverage_analysis": bg.coerce_list(bg.parse_jsonish(diagnosis.get("coverage_analysis"))),
        "repair_scope": repair_scope,
        "reason": str(diagnosis.get("reason") or ""),
        "structural_issue_types": bg.coerce_list(diagnostics.get("issue_types", [])),
        "structural_diagnostics": diagnostics,
        "candidate_repair_evidence": candidate_evidence,
        "allowed_operations": derive_allowed_operations(diagnosis, diagnostics),
        "forbidden_operations": [
            "DELETE_NODE",
            "REORDER_UNRELATED_NODES",
            "CHANGE_UNRELATED_TOOLS",
            "ADD_OPTIONAL_CONTEXT_EDGES",
            "INVENT_LITERAL_ARGUMENTS",
        ],
    }
    return suggestion


def derive_allowed_operations(diagnosis: Dict[str, Any] | None, diagnostics: Dict[str, Any]) -> List[str]:
    errors = set(bg.coerce_list((diagnosis or {}).get("error_types", [])))
    issues = set(bg.coerce_list(diagnostics.get("issue_types", [])))
    repair_scope = bg.parse_jsonish((diagnosis or {}).get("repair_scope"))
    if not isinstance(repair_scope, dict):
        repair_scope = {}
    allowed = ["FIX_ARGUMENTS"]
    if "type_incompatible" in issues or "wrong_link" in errors:
        allowed.extend(["DELETE_EDGE", "REPLACE_TOOL"])
    if "missing_required_slot" in issues:
        allowed.extend(["ADD_EDGE", "REPLACE_TOOL"])
    if "missing_intermediate_node" in errors or bool(repair_scope.get("requires_new_nodes", False)):
        allowed.extend(["INSERT_NODE"])
    if "wrong_tool" in errors or "unknown_tool" in issues or "unknown_tool" in errors:
        allowed.append("REPLACE_TOOL")
    if "coverage_missing" in errors or "wrong_decomposition" in errors:
        allowed.append("LOCAL_REPLAN_AFFECTED_SPAN")
    return bg.dedupe_preserve_order(allowed)


def should_repair(
    suggestion: Dict[str, Any],
    diagnostics: Dict[str, Any],
    selection_mode: str,
    allow_global_replan: bool,
) -> Tuple[bool, str]:
    verdict = str(suggestion.get("workflow_verdict") or "KEEP").upper()
    structural_issues = bg.coerce_list(diagnostics.get("issue_types", []))
    if verdict == "GLOBAL_REPLAN" and not allow_global_replan and selection_mode != "global_only":
        return False, "global_replan_not_allowed"
    if selection_mode == "structural_only":
        if verdict == "LOCAL_REPAIR" and structural_issues:
            return True, "LOCAL_REPAIR with structural diagnostics"
        return False, "not LOCAL_REPAIR with structural diagnostics"
    if selection_mode == "local_only":
        return (verdict == "LOCAL_REPAIR", "LOCAL_REPAIR selected" if verdict == "LOCAL_REPAIR" else "not LOCAL_REPAIR")
    if selection_mode == "all_non_keep":
        return (verdict != "KEEP", "non-KEEP selected" if verdict != "KEEP" else "KEEP skipped")
    if selection_mode == "global_only":
        return (verdict == "GLOBAL_REPLAN", "GLOBAL_REPLAN selected" if verdict == "GLOBAL_REPLAN" else "not GLOBAL_REPLAN")
    return False, f"unknown selection_mode={selection_mode}"


def base_output(
    case_id: str,
    user_request: str,
    original_result: Dict[str, Any],
    diagnosis: Dict[str, Any] | None,
    gpt_suggestion: Dict[str, Any],
    diagnostics: Dict[str, Any],
    original_edges: List[Dict[str, int]],
    selection_mode: str,
    selected: bool,
    selection_reason: str,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": original_result,
        "result": original_result,
        "selected_for_repair": selected,
        "selection_mode": selection_mode,
        "selection_reason": selection_reason,
        "qwen_repair_applied": False,
        "validation_status": "not_selected" if not selected else "pending",
        "validation_reject_reason": "",
        "workflow_verdict": gpt_suggestion.get("workflow_verdict", "KEEP"),
        "confidence": gpt_suggestion.get("confidence", "low"),
        "error_types": gpt_suggestion.get("error_types", []),
        "structural_issue_types": diagnostics.get("issue_types", []),
        "original_structural_error_count": diagnostics.get("structural_error_count", 0),
        "final_structural_error_count": diagnostics.get("structural_error_count", 0),
        "repair_scope": gpt_suggestion.get("repair_scope", {}),
        "qwen_repair_decision": "",
        "qwen_repair_operations": [],
        "deterministic_change_summary": {},
        "rejected_repair": {},
        "original_edges": original_edges,
        "proposed_edges": [],
        "final_edges": original_edges,
        "raw_qwen_repair_output": "",
        "warnings": warnings,
        "gpt_diagnosis": diagnosis or {},
        "qwen_proposed_result": None,
    }


def build_qwen_repair_prompt(
    user_request: str,
    baseline_workflow: Dict[str, Any],
    original_nodes: List[Dict[str, Any]],
    original_edges: List[Dict[str, int]],
    node_context: List[Dict[str, Any]],
    gpt_suggestion: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    max_tool_knowledge: int,
    max_new_nodes: int,
) -> str:
    payload = {
        "user_request": user_request,
        "baseline_workflow": baseline_workflow,
        "baseline_nodes_with_tool_knowledge": node_context,
        "normalized_edges": bg.enrich_edges(original_edges, original_nodes, tool_catalog, transition_index),
        "gpt_critic_suggestion": gpt_suggestion,
        "available_tools": dg.compact_tool_knowledge(tool_catalog, max_tool_knowledge),
        "repair_limits": {
            "max_new_nodes": max(0, int(max_new_nodes or 0)),
            "preserve_baseline_by_default": True,
            "return_original_if_uncertain": True,
        },
    }
    return f"""You are a Diagnosis-Guided Qwen Workflow Repair Agent.

You are repairing a baseline workflow generated by Qwen.
The GPT critic has already diagnosed the likely issue.

Your task is NOT to freely redesign the workflow.
Your task is to apply the GPT critic suggestion conservatively.

Default action:
Return KEEP_ORIGINAL and copy the baseline workflow.

Only return REPAIR when the GPT suggestion identifies a concrete local problem and you can fix it safely.

Hard constraints:
- Follow the GPT critic suggestion.
- Do NOT change unrelated nodes.
- Do NOT delete nodes.
- Do NOT reorder unrelated nodes.
- Do NOT add optional context edges.
- Do NOT optimize style.
- Use only tools from available_tools. If a baseline tool is unknown and there is no exact safe replacement, keep it.
- Preserve user-provided filenames, URLs, quoted text, effect names, speed values, voice/style parameters.
- Node references must use the repaired node indices in the form <node-i>.
- task_links must be consistent with <node-i> arguments.
- If you are uncertain, return KEEP_ORIGINAL.

Allowed operation types:
- FIX_ARGUMENTS
- DELETE_EDGE
- ADD_EDGE
- REPLACE_TOOL
- INSERT_NODE only when GPT says a required intermediate node is missing and repair_limits.max_new_nodes allows it.
- LOCAL_REPLAN_AFFECTED_SPAN only inside the GPT affected scope.

Return JSON only:
{{
  "repair_decision": "KEEP_ORIGINAL | REPAIR",
  "repaired_workflow": {{
    "task_steps": [...],
    "task_nodes": [
      {{"task": "...", "arguments": ["...", "<node-0>"]}}
    ],
    "task_links": [
      {{"source": "...", "target": "..."}}
    ]
  }},
  "repair_operations": [
    {{
      "op": "FIX_ARGUMENTS | DELETE_EDGE | ADD_EDGE | REPLACE_TOOL | INSERT_NODE | LOCAL_REPLAN_AFFECTED_SPAN",
      "affected_nodes": [0, 1],
      "affected_edges": [[0, 1]],
      "reason": "..."
    }}
  ],
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def call_json_repair(
    client: OpenAICompatibleLLMClient,
    prompt: str,
    warnings: List[str],
) -> Tuple[str, Dict[str, Any]]:
    raw_text = ""
    try:
        raw_text = client.chat(
            messages=[
                {"role": "system", "content": "You are a conservative workflow repair agent. Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        return raw_text, extract_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001 - row-level failure should not stop the run.
        warnings.append(f"qwen_repair_error: {type(exc).__name__}: {exc}")
        return raw_text, {}


def normalize_qwen_repair_payload(payload: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        warnings.append("qwen repair payload is not an object; keep original")
        return {"repair_decision": "KEEP_ORIGINAL", "repaired_workflow": None, "repair_operations": [], "reason": ""}
    decision = str(payload.get("repair_decision") or "").strip().upper()
    if decision not in ALLOWED_DECISIONS:
        warnings.append(f"invalid repair_decision={decision!r}; keep original")
        decision = "KEEP_ORIGINAL"
    workflow = bg.parse_jsonish(payload.get("repaired_workflow"))
    if not isinstance(workflow, dict) and "task_nodes" in payload:
        workflow = payload
    operations = bg.coerce_list(bg.parse_jsonish(payload.get("repair_operations")))
    operations = [item for item in operations if isinstance(item, dict)]
    return {
        "repair_decision": decision,
        "repaired_workflow": workflow if isinstance(workflow, dict) else None,
        "repair_operations": operations,
        "reason": str(payload.get("reason") or ""),
    }


def validate_qwen_repair(
    original_result: Dict[str, Any],
    original_nodes: List[Dict[str, Any]],
    original_edges: List[Dict[str, int]],
    proposed_result: Dict[str, Any] | None,
    diagnostics: Dict[str, Any],
    gpt_suggestion: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    user_request: str,
    max_new_nodes: int,
    require_structural_improvement: bool,
    warnings: List[str],
) -> Dict[str, Any]:
    if not isinstance(proposed_result, dict):
        return reject_validation("missing repaired_workflow", proposed_result, [], diagnostics)

    local_warnings: List[str] = []
    proposed_working = bg.ensure_result_shape(copy.deepcopy(proposed_result), local_warnings)
    proposed_nodes = bg.normalize_task_nodes(proposed_working, local_warnings)
    if not proposed_nodes and original_nodes:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation("proposed task_nodes is empty", proposed_working, [], diagnostics)

    node_count_delta = len(proposed_nodes) - len(original_nodes)
    if node_count_delta < 0:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation("node deletion is not allowed", proposed_working, [], diagnostics)
    if node_count_delta > max_new_nodes:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation(f"too many new nodes: {node_count_delta} > {max_new_nodes}", proposed_working, [], diagnostics)

    affected_nodes = derive_affected_nodes(gpt_suggestion, diagnostics, len(original_nodes))
    structure_reason = validate_node_change_scope(original_nodes, proposed_nodes, affected_nodes)
    if structure_reason:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation(structure_reason, proposed_working, [], diagnostics)

    tool_reason = validate_tools(original_nodes, proposed_nodes, tool_catalog)
    if tool_reason:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation(tool_reason, proposed_working, [], diagnostics)

    proposed_edges = bg.normalize_workflow_edges(proposed_working, proposed_nodes, local_warnings)
    edge_reason = validate_edges(proposed_nodes, proposed_edges, tool_catalog)
    if edge_reason:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation(edge_reason, proposed_working, proposed_edges, diagnostics)

    proposed_diagnostics = dg.build_structural_diagnostics(proposed_nodes, proposed_edges, tool_catalog)
    original_error_count = int(diagnostics.get("structural_error_count", 0) or 0)
    proposed_error_count = int(proposed_diagnostics.get("structural_error_count", 0) or 0)
    if require_structural_improvement and original_error_count > 0 and proposed_error_count >= original_error_count:
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation(
            f"no structural improvement: {proposed_error_count} >= {original_error_count}",
            proposed_working,
            proposed_edges,
            diagnostics,
            final_structural_error_count=proposed_error_count,
        )

    repaired_result = bg.rebuild_result(proposed_working, proposed_nodes, proposed_edges, [], user_request)
    change_summary = build_change_summary(original_result, repaired_result, original_edges, proposed_edges)
    if not change_summary.get("has_change"):
        warnings.extend(prefix_warnings("proposed", local_warnings))
        return reject_validation("repair has no deterministic change", proposed_working, proposed_edges, diagnostics)

    warnings.extend(prefix_warnings("proposed", local_warnings))
    return {
        "accepted": True,
        "status": "accepted",
        "reject_reason": "",
        "repaired_result": repaired_result,
        "qwen_proposed_result": proposed_working,
        "proposed_edges": proposed_edges,
        "final_edges": proposed_edges,
        "change_summary": change_summary,
        "final_structural_error_count": proposed_error_count,
        "rejected_repair": {},
    }


def reject_validation(
    reason: str,
    proposed_result: Any,
    proposed_edges: List[Dict[str, int]],
    diagnostics: Dict[str, Any],
    final_structural_error_count: int | None = None,
) -> Dict[str, Any]:
    return {
        "accepted": False,
        "status": "rejected",
        "reject_reason": reason,
        "qwen_proposed_result": proposed_result,
        "proposed_edges": proposed_edges,
        "final_edges": [],
        "change_summary": {},
        "final_structural_error_count": (
            final_structural_error_count
            if final_structural_error_count is not None
            else diagnostics.get("structural_error_count", 0)
        ),
        "rejected_repair": {"reason": reason, "proposed_result": proposed_result, "proposed_edges": proposed_edges},
    }


def derive_affected_nodes(
    suggestion: Dict[str, Any],
    diagnostics: Dict[str, Any],
    original_node_count: int,
) -> set[int]:
    affected: set[int] = set()
    repair_scope = suggestion.get("repair_scope") if isinstance(suggestion.get("repair_scope"), dict) else {}
    for raw in bg.coerce_list(repair_scope.get("affected_nodes", [])):
        value = bg.coerce_int(raw)
        if value is not None and 0 <= value < original_node_count:
            affected.add(value)
    for edge in bg.coerce_list(repair_scope.get("affected_edges", [])):
        pair = dg.normalize_edge_pair(edge)
        if pair is None:
            continue
        for value in pair:
            if 0 <= value < original_node_count:
                affected.add(value)
    for edge in bg.coerce_list(diagnostics.get("type_incompatible_edges", [])):
        for key in ("source", "target"):
            value = bg.coerce_int(edge.get(key)) if isinstance(edge, dict) else None
            if value is not None and 0 <= value < original_node_count:
                affected.add(value)
    for item in bg.coerce_list(diagnostics.get("missing_required_slots", [])):
        value = bg.coerce_int(item.get("node_index")) if isinstance(item, dict) else None
        if value is not None and 0 <= value < original_node_count:
            affected.add(value)
    for item in bg.coerce_list(diagnostics.get("invalid_node_refs", [])):
        value = bg.coerce_int(item.get("target")) if isinstance(item, dict) else None
        if value is not None and 0 <= value < original_node_count:
            affected.add(value)
    for item in bg.coerce_list(diagnostics.get("unknown_tools", [])):
        value = bg.coerce_int(item.get("node_index")) if isinstance(item, dict) else None
        if value is not None and 0 <= value < original_node_count:
            affected.add(value)
    if not affected and str(suggestion.get("workflow_verdict") or "").upper() == "LOCAL_REPAIR":
        # If GPT did not give scope, keep the validator permissive enough to inspect the whole local proposal.
        affected.update(range(original_node_count))
    return affected


def validate_node_change_scope(
    original_nodes: List[Dict[str, Any]],
    proposed_nodes: List[Dict[str, Any]],
    affected_nodes: set[int],
) -> str:
    if len(proposed_nodes) == len(original_nodes):
        for index, original_node in enumerate(original_nodes):
            old_task = bg.normalize_tool_key(original_node.get("task", ""))
            new_task = bg.normalize_tool_key(proposed_nodes[index].get("task", ""))
            if index not in affected_nodes and old_task != new_task:
                return f"unrelated tool changed at node {index}"
        return ""

    # Node insertion is allowed only if unaffected baseline tools remain an ordered subsequence.
    proposed_cursor = 0
    for original_index, original_node in enumerate(original_nodes):
        if original_index in affected_nodes:
            continue
        old_task = bg.normalize_tool_key(original_node.get("task", ""))
        found = False
        while proposed_cursor < len(proposed_nodes):
            if bg.normalize_tool_key(proposed_nodes[proposed_cursor].get("task", "")) == old_task:
                found = True
                proposed_cursor += 1
                break
            proposed_cursor += 1
        if not found:
            return f"unaffected baseline node {original_index} is not preserved in order"
    return ""


def validate_tools(
    original_nodes: List[Dict[str, Any]],
    proposed_nodes: List[Dict[str, Any]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> str:
    original_unknown = {
        bg.normalize_tool_key(node.get("task", ""))
        for node in original_nodes
        if not bg.tool_is_known(bg.lookup_tool(tool_catalog, node.get("task", "")))
    }
    for index, node in enumerate(proposed_nodes):
        task = node.get("task", "")
        tool = bg.lookup_tool(tool_catalog, task)
        if bg.tool_is_known(tool):
            continue
        if bg.normalize_tool_key(task) in original_unknown:
            continue
        return f"unknown proposed tool at node {index}: {task}"
    return ""


def validate_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> str:
    for edge in edges:
        source = bg.coerce_int(edge.get("source"))
        target = bg.coerce_int(edge.get("target"))
        if source is None or target is None or not bg.is_valid_edge_tuple(source, target, len(nodes)):
            return f"invalid edge {source}->{target}"
        source_tool = bg.lookup_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = bg.lookup_tool(tool_catalog, nodes[target].get("task", ""))
        if not bg.tool_is_known(source_tool) or not bg.tool_is_known(target_tool):
            continue
        if bg.type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", [])) is False:
            return f"type incompatible edge after repair: {source}->{target}"
    return ""


def build_change_summary(
    original_result: Dict[str, Any],
    repaired_result: Dict[str, Any],
    original_edges: List[Dict[str, int]],
    final_edges: List[Dict[str, int]],
) -> Dict[str, Any]:
    original_nodes = original_result.get("task_nodes", []) if isinstance(original_result.get("task_nodes"), list) else []
    repaired_nodes = repaired_result.get("task_nodes", []) if isinstance(repaired_result.get("task_nodes"), list) else []
    changed_tools = []
    changed_arguments = []
    for index in range(min(len(original_nodes), len(repaired_nodes))):
        old_tool = str(original_nodes[index].get("task") or "")
        new_tool = str(repaired_nodes[index].get("task") or "")
        if old_tool != new_tool:
            changed_tools.append({"node_index": index, "old_tool": old_tool, "new_tool": new_tool})
        old_args = original_nodes[index].get("arguments", [])
        new_args = repaired_nodes[index].get("arguments", [])
        if old_args != new_args:
            changed_arguments.append({"node_index": index, "old_arguments": old_args, "new_arguments": new_args})
    added_nodes = repaired_nodes[len(original_nodes) :] if len(repaired_nodes) > len(original_nodes) else []
    removed_nodes = original_nodes[len(repaired_nodes) :] if len(repaired_nodes) < len(original_nodes) else []
    old_edges = bg.edge_key_set(original_edges)
    new_edges = bg.edge_key_set(final_edges)
    added_edges = bg.edge_tuples_to_dicts(sorted(new_edges - old_edges, key=lambda item: (item[1], item[0])))
    removed_edges = bg.edge_tuples_to_dicts(sorted(old_edges - new_edges, key=lambda item: (item[1], item[0])))
    return {
        "has_change": bool(changed_tools or changed_arguments or added_nodes or removed_nodes or added_edges or removed_edges),
        "changed_tools": changed_tools,
        "changed_arguments": changed_arguments,
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "added_edges": added_edges,
        "removed_edges": removed_edges,
    }


def prefix_warnings(prefix: str, warnings: List[str]) -> List[str]:
    return [f"{prefix}: {warning}" for warning in warnings]


def error_result(
    case_id: str,
    user_request: str,
    original_result: Any,
    diagnosis: Dict[str, Any] | None,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": original_result,
        "result": original_result,
        "selected_for_repair": False,
        "selection_mode": "",
        "selection_reason": "",
        "qwen_repair_applied": False,
        "validation_status": "error",
        "validation_reject_reason": "",
        "workflow_verdict": "KEEP",
        "confidence": "low",
        "error_types": [],
        "structural_issue_types": [],
        "original_structural_error_count": 0,
        "final_structural_error_count": 0,
        "repair_scope": {},
        "qwen_repair_decision": "",
        "qwen_repair_operations": [],
        "deterministic_change_summary": {},
        "rejected_repair": {},
        "original_edges": [],
        "proposed_edges": [],
        "final_edges": [],
        "raw_qwen_repair_output": "",
        "warnings": warnings,
        "gpt_diagnosis": diagnosis or {},
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
