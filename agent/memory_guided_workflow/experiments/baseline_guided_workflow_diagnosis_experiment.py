# -*- coding: utf-8 -*-
"""Baseline-guided workflow diagnosis experiment.

This script uses an LLM critic to diagnose an existing Qwen/LLM workflow.
It does not repair or replan the workflow. Gold labels are intentionally not
used here; they should only be used in separate offline analysis.
"""

from __future__ import annotations

import argparse
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
from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_JSONL = "agent/memory_guided_workflow/outputs/baseline_guided_workflow_diagnosis.jsonl"
DEFAULT_OUTPUT_XLSX = "agent/memory_guided_workflow/outputs/baseline_guided_workflow_diagnosis.xlsx"
ALLOWED_VERDICTS = {"KEEP", "LOCAL_REPAIR", "GLOBAL_REPLAN"}
ALLOWED_CONFIDENCE = {"low", "medium", "high"}
ALLOWED_ERROR_TYPES = {
    "coverage_missing",
    "wrong_decomposition",
    "wrong_tool",
    "wrong_link",
    "missing_intermediate_node",
    "structural_error",
    "argument_error",
    "unknown_tool",
}
XLSX_HEADERS = [
    "id",
    "user_request",
    "workflow_verdict",
    "confidence",
    "error_types",
    "rule_based_verdict",
    "structural_issue_types",
    "structural_diagnostics",
    "repair_scope",
    "coverage_analysis",
    "reason",
    "normalized_edges",
    "node_context",
    "candidate_repair_evidence",
    "raw_diagnosis_output",
    "warnings",
    "original_result",
]
_THREAD_LOCAL = threading.local()


def main() -> int:
    args = parse_args()
    rows = bg.read_input_records(resolve_input_path(args), input_kind(args))
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    output_jsonl = bg.resolve_path(args.output_jsonl)
    output_xlsx = bg.resolve_path(args.output_xlsx)
    completed_ids = bg.load_completed_ids(output_jsonl) if args.resume else set()
    results = bg.read_jsonl_records(output_jsonl) if args.resume and output_jsonl.exists() else []

    tool_catalog = bg.load_tool_catalog(bg.resolve_path(args.tool_desc))
    transition_index = bg.load_transition_index(args.transition_graph)
    pending: List[Tuple[int, Dict[str, Any], str]] = []
    for row_index, row in enumerate(rows, start=1):
        case_id = bg.get_case_id(row, row_index)
        if args.resume and case_id in completed_ids:
            print(f"[{row_index}/{len(rows)}] skip id={case_id} (resume)")
            continue
        pending.append((row_index, row, case_id))

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row_index, row, case_id in pending:
            print(f"[{row_index}/{len(rows)}] diagnose id={case_id}")
            _, _, result = run_case_worker(
                row_index,
                row,
                case_id,
                args.llm_config,
                args.llm_profile,
                tool_catalog,
                transition_index,
                args.max_tool_knowledge,
            )
            bg.append_jsonl(output_jsonl, result)
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
                    args.llm_config,
                    args.llm_profile,
                    tool_catalog,
                    transition_index,
                    args.max_tool_knowledge,
                ): (row_index, case_id)
                for row_index, row, case_id in pending
            }
            for future in as_completed(futures):
                row_index, case_id = futures[future]
                try:
                    _, _, result = future.result()
                except Exception as exc:  # noqa: BLE001 - preserve row-level output.
                    result = error_result(case_id, "", None, [f"worker_error: {type(exc).__name__}: {exc}"])
                bg.append_jsonl(output_jsonl, result)
                results.append(result)
                print(f"[{row_index}/{len(rows)}] done id={case_id}")

    write_xlsx(output_xlsx, results)
    print(f"saved_jsonl={output_jsonl}")
    print(f"saved_xlsx={output_xlsx}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baseline-guided workflow diagnosis experiment.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input-json", default=None)
    group.add_argument("--input-jsonl", default=None)
    group.add_argument("--input-xlsx", default=None)
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--transition-graph", default=None)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--llm-config", default="configs/openai.json")
    parser.add_argument("--llm-profile", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--max-tool-knowledge", type=int, default=80)
    parser.add_argument("--mode", choices=["diagnose_only"], default="diagnose_only")
    return parser.parse_args()


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
    llm_config: Any,
    llm_profile: str | None,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    max_tool_knowledge: int,
) -> Tuple[int, str, Dict[str, Any]]:
    client = get_thread_client(llm_config, llm_profile)
    try:
        result = run_one_case(row, case_id, client, tool_catalog, transition_index, max_tool_knowledge)
    except Exception as exc:  # noqa: BLE001 - keep row-level output.
        user_request = bg.get_first_value(row, ["user_request", "request", "instruction", "User Request"])
        result = error_result(case_id, user_request, None, [f"case_error: {type(exc).__name__}: {exc}"])
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
    client: OpenAICompatibleLLMClient,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    max_tool_knowledge: int,
) -> Dict[str, Any]:
    warnings: List[str] = []
    user_request = bg.get_first_value(row, ["user_request", "request", "instruction", "User Request"])
    original_result = bg.extract_baseline_result(row, warnings)
    if original_result is None:
        return error_result(case_id, user_request, None, warnings + ["missing workflow result"])

    task_nodes = bg.normalize_task_nodes(original_result, warnings)
    normalized_edges = bg.normalize_workflow_edges(original_result, task_nodes, warnings)
    diagnostics = build_structural_diagnostics(task_nodes, normalized_edges, tool_catalog)
    node_context = bg.build_node_context(task_nodes, tool_catalog)
    candidate_evidence = build_candidate_repair_evidence(task_nodes, normalized_edges, diagnostics, tool_catalog, transition_index)
    rule_verdict = rule_based_verdict(diagnostics)

    prompt = build_diagnosis_prompt(
        user_request=user_request,
        baseline_workflow=original_result,
        node_context=node_context,
        normalized_edges=normalized_edges,
        diagnostics=diagnostics,
        candidate_evidence=candidate_evidence,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        max_tool_knowledge=max_tool_knowledge,
    )
    raw_output, payload = call_json_critic(client, prompt, warnings)
    diagnosis = normalize_diagnosis_payload(payload, warnings)

    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "workflow_verdict": diagnosis["workflow_verdict"],
        "confidence": diagnosis["confidence"],
        "error_types": diagnosis["error_types"],
        "coverage_analysis": diagnosis["coverage_analysis"],
        "repair_scope": diagnosis["repair_scope"],
        "reason": diagnosis["reason"],
        "rule_based_verdict": rule_verdict,
        "structural_issue_types": diagnostics["issue_types"],
        "structural_diagnostics": diagnostics,
        "normalized_edges": normalized_edges,
        "node_context": node_context,
        "candidate_repair_evidence": candidate_evidence,
        "raw_diagnosis_output": raw_output,
        "warnings": warnings,
    }


def error_result(case_id: str, user_request: str, original_result: Any, warnings: List[str]) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "workflow_verdict": "KEEP",
        "confidence": "low",
        "error_types": [],
        "coverage_analysis": [],
        "repair_scope": default_repair_scope(),
        "reason": "",
        "rule_based_verdict": "KEEP",
        "structural_issue_types": [],
        "structural_diagnostics": {},
        "normalized_edges": [],
        "node_context": [],
        "candidate_repair_evidence": {},
        "raw_diagnosis_output": "",
        "warnings": warnings,
    }


def call_json_critic(
    client: OpenAICompatibleLLMClient,
    prompt: str,
    warnings: List[str],
) -> Tuple[str, Dict[str, Any]]:
    raw_text = ""
    try:
        raw_text = client.chat(
            messages=[
                {"role": "system", "content": "You are a strict workflow diagnosis critic. Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        return raw_text, extract_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001 - row-level failure should not stop the run.
        warnings.append(f"diagnosis_error: {type(exc).__name__}: {exc}")
        return raw_text, {}


def build_diagnosis_prompt(
    user_request: str,
    baseline_workflow: Dict[str, Any],
    node_context: List[Dict[str, Any]],
    normalized_edges: List[Dict[str, int]],
    diagnostics: Dict[str, Any],
    candidate_evidence: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    max_tool_knowledge: int,
) -> str:
    payload = {
        "user_request": user_request,
        "baseline_workflow": baseline_workflow,
        "workflow_nodes_with_tool_knowledge": node_context,
        "normalized_edges": bg.enrich_edges(normalized_edges, baseline_workflow.get("task_nodes", []), tool_catalog, transition_index),
        "structural_diagnostics": diagnostics,
        "candidate_repair_evidence": candidate_evidence,
        "available_tools": compact_tool_knowledge(tool_catalog, max_tool_knowledge),
    }
    return f"""You are a Baseline-Guided Workflow Diagnosis Critic.

You are reviewing a workflow generated by another model.

Your task is NOT to repair the workflow.
Your task is NOT to generate a new workflow.
Your task is ONLY to diagnose whether the baseline workflow should be:

1. KEEP
   The baseline workflow is structurally valid and semantically covers the user request.

2. LOCAL_REPAIR
   The workflow is mostly useful, but a local segment is wrong.
   Examples: type-incompatible edge, invalid node reference, missing intermediate node,
   missing required input, wrong local tool, or wrong local link.

3. GLOBAL_REPLAN
   The task decomposition or workflow semantics are broadly wrong.
   Examples: missing major request coverage, wrong task decomposition, wrong modality path,
   or a workflow that is internally plausible but does not satisfy the user request.

Default behavior:
- Prefer KEEP when the workflow is plausible and no strong evidence of error exists.
- Prefer LOCAL_REPAIR for localized structural errors.
- Use GLOBAL_REPLAN only when the baseline decomposition is clearly wrong.

Important constraints:
- Do not optimize style.
- Do not propose a repaired workflow.
- Do not add/delete/reorder nodes in your answer.
- Do not use gold labels; judge only from the user request, baseline workflow, tool knowledge,
  structural diagnostics, and transition evidence.

Allowed error_types:
- coverage_missing
- wrong_decomposition
- wrong_tool
- wrong_link
- missing_intermediate_node
- structural_error
- argument_error
- unknown_tool

Return JSON only:
{{
  "workflow_verdict": "KEEP | LOCAL_REPAIR | GLOBAL_REPLAN",
  "confidence": "low | medium | high",
  "error_types": ["..."],
  "coverage_analysis": [
    {{
      "request_part": "...",
      "status": "covered | missing | wrong_tool | wrong_order | unclear",
      "covered_by_nodes": [0, 1],
      "reason": "..."
    }}
  ],
  "repair_scope": {{
    "affected_nodes": [1, 2],
    "affected_edges": [[0, 1]],
    "requires_new_nodes": false,
    "requires_global_replan": false
  }},
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_structural_diagnostics(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    unknown_tools = []
    for index, node in enumerate(nodes):
        tool = bg.lookup_tool(tool_catalog, node.get("task", ""))
        if not bg.tool_is_known(tool):
            unknown_tools.append({"node_index": index, "tool": str(node.get("task") or "")})

    invalid_refs = scan_invalid_node_refs(nodes)
    type_incompatible_edges = []
    for edge in edges:
        source_tool = bg.lookup_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
        target_tool = bg.lookup_tool(tool_catalog, nodes[edge["target"]].get("task", ""))
        if not bg.tool_is_known(source_tool) or not bg.tool_is_known(target_tool):
            continue
        compatibility = bg.type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", []))
        if compatibility is False:
            type_incompatible_edges.append(
                {
                    "source": edge["source"],
                    "target": edge["target"],
                    "source_tool": source_tool.get("id", ""),
                    "target_tool": target_tool.get("id", ""),
                    "source_output_types": source_tool.get("output_types", []),
                    "target_input_types": target_tool.get("input_types", []),
                }
            )

    missing_required_slots = []
    for index, node in enumerate(nodes):
        tool = bg.lookup_tool(tool_catalog, node.get("task", ""))
        if not bg.tool_is_known(tool):
            continue
        status = bg.input_slot_status_for_target(index, nodes, edges, tool_catalog)
        if status.get("missing_slots"):
            missing_required_slots.append(
                {
                    "node_index": index,
                    "tool": str(node.get("task") or ""),
                    "required_slots": status.get("required_slots", []),
                    "satisfied_slots": status.get("satisfied_slots", []),
                    "missing_slots": status.get("missing_slots", []),
                }
            )

    issue_types = []
    if type_incompatible_edges:
        issue_types.append("type_incompatible")
    if missing_required_slots:
        issue_types.append("missing_required_slot")
    if invalid_refs:
        issue_types.append("invalid_node_reference")
    if unknown_tools:
        issue_types.append("unknown_tool")

    return {
        "issue_types": issue_types,
        "type_incompatible_edges": type_incompatible_edges,
        "missing_required_slots": missing_required_slots,
        "invalid_node_refs": invalid_refs,
        "unknown_tools": unknown_tools,
        "structural_error_count": (
            len(type_incompatible_edges)
            + len(missing_required_slots)
            + len(invalid_refs)
            + len(unknown_tools)
        ),
    }


def scan_invalid_node_refs(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    invalid = []
    node_count = len(nodes)
    for target, node in enumerate(nodes):
        arguments = node.get("arguments", [])
        if not isinstance(arguments, list):
            arguments = [] if arguments is None else [arguments]
        for argument in arguments:
            source = bg.parse_node_ref(argument)
            if source is None:
                continue
            reason = ""
            if source < 0 or source >= node_count:
                reason = "ref_out_of_range"
            elif source >= target:
                reason = "ref_not_previous"
            if reason:
                invalid.append({"source": source, "target": target, "argument": argument, "reason": reason})
    return invalid


def build_candidate_repair_evidence(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    diagnostics: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> Dict[str, Any]:
    return {
        "intermediate_tool_candidates": build_intermediate_tool_candidates(
            nodes,
            diagnostics.get("type_incompatible_edges", []),
            tool_catalog,
            transition_index,
        ),
        "missing_slot_source_candidates": build_missing_slot_source_candidates(
            nodes,
            edges,
            diagnostics.get("missing_required_slots", []),
            tool_catalog,
            transition_index,
        ),
    }


def build_intermediate_tool_candidates(
    nodes: List[Dict[str, Any]],
    incompatible_edges: List[Dict[str, Any]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> List[Dict[str, Any]]:
    candidates = []
    tools = [tool for tool in tool_catalog.values() if bg.tool_is_known(tool)]
    for edge in incompatible_edges:
        source = edge["source"]
        target = edge["target"]
        source_tool = bg.lookup_tool(tool_catalog, nodes[source].get("task", ""))
        target_tool = bg.lookup_tool(tool_catalog, nodes[target].get("task", ""))
        edge_candidates = []
        for tool in tools:
            if tool["id"] in {source_tool.get("id"), target_tool.get("id")}:
                continue
            left_ok = bg.type_compatible(source_tool.get("output_types", []), tool.get("input_types", [])) is True
            right_ok = bg.type_compatible(tool.get("output_types", []), target_tool.get("input_types", [])) is True
            if not (left_ok and right_ok):
                continue
            p_left = bg.get_transition_probability(transition_index, source_tool.get("id", ""), tool.get("id", ""))
            p_right = bg.get_transition_probability(transition_index, tool.get("id", ""), target_tool.get("id", ""))
            edge_candidates.append(
                {
                    "tool": tool.get("id", ""),
                    "intent": tool.get("intent", ""),
                    "input_types": tool.get("input_types", []),
                    "output_types": tool.get("output_types", []),
                    "source_to_candidate_probability": p_left,
                    "candidate_to_target_probability": p_right,
                    "score": transition_score(p_left) + transition_score(p_right),
                }
            )
        edge_candidates.sort(key=lambda item: item["score"], reverse=True)
        candidates.append(
            {
                "source": source,
                "target": target,
                "source_tool": source_tool.get("id", ""),
                "target_tool": target_tool.get("id", ""),
                "candidate_intermediate_tools": edge_candidates[:5],
            }
        )
    return candidates


def build_missing_slot_source_candidates(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    missing_slots: List[Dict[str, Any]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> List[Dict[str, Any]]:
    existing = bg.edge_key_set(edges)
    candidates = []
    for item in missing_slots:
        target = item["node_index"]
        target_tool = bg.lookup_tool(tool_catalog, nodes[target].get("task", ""))
        source_candidates = []
        for source in range(target):
            if (source, target) in existing:
                continue
            source_tool = bg.lookup_tool(tool_catalog, nodes[source].get("task", ""))
            if not bg.tool_is_known(source_tool):
                continue
            source_types = bg.normalize_type_set(source_tool.get("output_types", []))
            if not source_types.intersection(bg.normalize_type_set(item.get("missing_slots", []))):
                continue
            p = bg.get_transition_probability(transition_index, source_tool.get("id", ""), target_tool.get("id", ""))
            source_candidates.append(
                {
                    "source": source,
                    "source_tool": source_tool.get("id", ""),
                    "source_output_types": source_tool.get("output_types", []),
                    "transition_probability": p,
                    "score": transition_score(p),
                }
            )
        source_candidates.sort(key=lambda row: row["score"], reverse=True)
        candidates.append(
            {
                "target": target,
                "target_tool": target_tool.get("id", ""),
                "missing_slots": item.get("missing_slots", []),
                "candidate_sources": source_candidates[:5],
            }
        )
    return candidates


def transition_score(value: Any) -> float:
    number = bg.coerce_float(value)
    return number if number is not None else 0.0


def rule_based_verdict(diagnostics: Dict[str, Any]) -> str:
    issue_types = set(diagnostics.get("issue_types", []))
    if not issue_types:
        return "KEEP"
    if len(issue_types) > 1 or "invalid_node_reference" in issue_types:
        return "LOCAL_REPAIR"
    if "type_incompatible" in issue_types or "missing_required_slot" in issue_types:
        return "LOCAL_REPAIR"
    if "unknown_tool" in issue_types:
        return "LOCAL_REPAIR"
    return "KEEP"


def normalize_diagnosis_payload(payload: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        warnings.append("diagnosis payload is not an object; default to KEEP")
        return default_diagnosis()
    verdict = str(payload.get("workflow_verdict") or "").strip().upper()
    if verdict not in ALLOWED_VERDICTS:
        warnings.append(f"invalid workflow_verdict={verdict!r}; default to KEEP")
        verdict = "KEEP"
    confidence = str(payload.get("confidence") or "").strip().lower()
    if confidence not in ALLOWED_CONFIDENCE:
        confidence = "low"
    error_types = []
    for item in bg.coerce_list(payload.get("error_types", [])):
        text = str(item or "").strip()
        if text in ALLOWED_ERROR_TYPES and text not in error_types:
            error_types.append(text)
    coverage_analysis = []
    for item in bg.coerce_list(payload.get("coverage_analysis", [])):
        if not isinstance(item, dict):
            continue
        covered_by_nodes = [
            value
            for value in (bg.coerce_int(raw) for raw in bg.coerce_list(item.get("covered_by_nodes", [])))
            if value is not None
        ]
        coverage_analysis.append(
            {
                "request_part": str(item.get("request_part") or ""),
                "status": str(item.get("status") or "unclear"),
                "covered_by_nodes": covered_by_nodes,
                "reason": str(item.get("reason") or ""),
            }
        )
    repair_scope = normalize_repair_scope(payload.get("repair_scope"))
    return {
        "workflow_verdict": verdict,
        "confidence": confidence,
        "error_types": error_types,
        "coverage_analysis": coverage_analysis,
        "repair_scope": repair_scope,
        "reason": str(payload.get("reason") or ""),
    }


def normalize_repair_scope(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        return default_repair_scope()
    affected_nodes = [
        node
        for node in (bg.coerce_int(raw) for raw in bg.coerce_list(value.get("affected_nodes", [])))
        if node is not None
    ]
    affected_edges = []
    for edge in bg.coerce_list(value.get("affected_edges", [])):
        normalized = normalize_edge_pair(edge)
        if normalized is not None:
            affected_edges.append(normalized)
    return {
        "affected_nodes": bg.dedupe_preserve_order(affected_nodes),
        "affected_edges": bg.dedupe_preserve_order(affected_edges),
        "requires_new_nodes": bool(value.get("requires_new_nodes", False)),
        "requires_global_replan": bool(value.get("requires_global_replan", False)),
    }


def normalize_edge_pair(value: Any) -> List[int] | None:
    if isinstance(value, dict):
        source = bg.coerce_int(value.get("source"))
        target = bg.coerce_int(value.get("target"))
    else:
        values = bg.coerce_list(value)
        if len(values) < 2:
            return None
        source = bg.coerce_int(values[0])
        target = bg.coerce_int(values[1])
    if source is None or target is None:
        return None
    return [source, target]


def default_diagnosis() -> Dict[str, Any]:
    return {
        "workflow_verdict": "KEEP",
        "confidence": "low",
        "error_types": [],
        "coverage_analysis": [],
        "repair_scope": default_repair_scope(),
        "reason": "",
    }


def default_repair_scope() -> Dict[str, Any]:
    return {
        "affected_nodes": [],
        "affected_edges": [],
        "requires_new_nodes": False,
        "requires_global_replan": False,
    }


def compact_tool_knowledge(tool_catalog: Dict[str, Dict[str, Any]], max_tools: int) -> List[Dict[str, Any]]:
    tools = sorted(tool_catalog.values(), key=lambda item: str(item.get("id", "")))
    result = []
    for tool in tools[: max(1, max_tools)]:
        result.append(
            {
                "id": tool.get("id", ""),
                "desc": tool.get("desc", ""),
                "input_types": tool.get("input_types", []),
                "output_types": tool.get("output_types", []),
                "intent": tool.get("intent", ""),
            }
        )
    return result


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
