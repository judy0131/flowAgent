# -*- coding: utf-8 -*-
"""Baseline-guided MIWP workflow critic experiment.

This script refines an existing LLM/Qwen workflow conservatively. It is an
offline experiment and does not touch the MIWP runtime, TaskUnderstanding, or
IncrementalPlanner.
"""

from __future__ import annotations

import argparse
import ast
import copy
import html
import json
import re
import sys
import threading
import zipfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_JSONL = "agent/memory_guided_workflow/outputs/baseline_guided_workflow_critic.jsonl"
DEFAULT_OUTPUT_XLSX = "agent/memory_guided_workflow/outputs/baseline_guided_workflow_critic.xlsx"
NODE_REF_RE = re.compile(r"^<node-(\d+)>$")
XML_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
MODES = (
    "delete_only",
    "tool_only",
    "edge_only",
    "argument_only",
    "tool_edge",
    "edge_argument",
    "tool_edge_argument",
)
_THREAD_LOCAL = threading.local()
XLSX_HEADERS = [
    "id",
    "user_request",
    "tool_operations",
    "edge_remove_operations",
    "edge_add_operations",
    "argument_operations",
    "rejected_tool_operations",
    "rejected_add_edges",
    "original_edges",
    "final_edges",
    "warnings",
    "original_result",
    "repaired_result",
    "raw_tool_critic_output",
    "raw_edge_delete_output",
    "raw_edge_add_output",
    "raw_argument_critic_output",
]


def main() -> int:
    args = parse_args()
    rows = read_input_records(resolve_input_path(args), input_kind(args))
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    output_jsonl = resolve_path(args.output_jsonl)
    output_xlsx = resolve_path(args.output_xlsx)
    completed_ids = load_completed_ids(output_jsonl) if args.resume else set()
    results = read_jsonl_records(output_jsonl) if args.resume and output_jsonl.exists() else []

    tool_catalog = load_tool_catalog(resolve_path(args.tool_desc))
    transition_index = load_transition_index(args.transition_graph)
    enabled = enabled_stages(args)

    pending: List[Tuple[int, Dict[str, Any], str]] = []
    for row_index, row in enumerate(rows, start=1):
        case_id = get_case_id(row, row_index)
        if args.resume and case_id in completed_ids:
            print(f"[{row_index}/{len(rows)}] skip id={case_id} (resume)")
            continue
        pending.append((row_index, row, case_id))

    workers = max(1, int(args.workers or 1))
    if workers == 1:
        for row_index, row, case_id in pending:
            print(f"[{row_index}/{len(rows)}] critic id={case_id}")
            _, _, result = run_case_worker(
                row_index,
                row,
                case_id,
                args.llm_config,
                args.llm_profile,
                tool_catalog,
                transition_index,
                enabled,
            )
            append_jsonl(output_jsonl, result)
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
                    args.llm_config,
                    args.llm_profile,
                    tool_catalog,
                    transition_index,
                    enabled,
                ): (row_index, case_id)
                for row_index, row, case_id in pending
            }
            for future in as_completed(futures):
                row_index, case_id = futures[future]
                try:
                    _, _, result = future.result()
                except Exception as exc:  # noqa: BLE001 - protect the output stream.
                    result = error_result(case_id, "", None, [f"worker_error: {type(exc).__name__}: {exc}"])
                append_jsonl(output_jsonl, result)
                completed_ids.add(case_id)
                results.append(result)
                print(f"[{row_index}/{len(rows)}] done id={case_id}")

    write_xlsx(output_xlsx, results)
    print(f"saved_jsonl={output_jsonl}")
    print(f"saved_xlsx={output_xlsx}")
    return 0


def run_case_worker(
    row_index: int,
    row: Dict[str, Any],
    case_id: str,
    llm_config: Any,
    llm_profile: str | None,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    enabled: Dict[str, bool],
) -> Tuple[int, str, Dict[str, Any]]:
    client = get_thread_client(llm_config, llm_profile)
    try:
        result = run_one_case(row, case_id, client, tool_catalog, transition_index, enabled)
    except Exception as exc:  # noqa: BLE001 - keep row-level output.
        user_request = get_first_value(row, ["user_request", "request", "instruction", "User Request"])
        result = error_result(case_id, user_request, None, [f"case_error: {type(exc).__name__}: {exc}"])
    return row_index, case_id, result


def get_thread_client(llm_config: Any, llm_profile: str | None) -> OpenAICompatibleLLMClient:
    signature = (str(llm_config or ""), str(llm_profile or ""))
    if getattr(_THREAD_LOCAL, "client_signature", None) != signature:
        _THREAD_LOCAL.client = OpenAICompatibleLLMClient(llm_config_path=llm_config, llm_profile=llm_profile)
        _THREAD_LOCAL.client_signature = signature
    return _THREAD_LOCAL.client


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Baseline-guided MIWP workflow critic experiment.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input-json", default=None)
    group.add_argument("--input-jsonl", default=None)
    group.add_argument("--input-xlsx", default=None)
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--transition-graph", default=None)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--llm-config", default="configs/qwen.json")
    parser.add_argument("--llm-profile", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent case workers.")
    parser.add_argument("--disable-tool-critic", action="store_true")
    parser.add_argument("--disable-edge-critic", action="store_true")
    parser.add_argument("--disable-argument-critic", action="store_true")
    parser.add_argument("--mode", choices=MODES, default="tool_edge_argument")
    return parser.parse_args()


def enabled_stages(args: argparse.Namespace) -> Dict[str, bool]:
    mapping = {
        "delete_only": {"tool": False, "edge_delete": True, "edge_add": False, "argument": False},
        "tool_only": {"tool": True, "edge_delete": False, "edge_add": False, "argument": False},
        "edge_only": {"tool": False, "edge_delete": True, "edge_add": True, "argument": False},
        "argument_only": {"tool": False, "edge_delete": False, "edge_add": False, "argument": True},
        "tool_edge": {"tool": True, "edge_delete": True, "edge_add": True, "argument": False},
        "edge_argument": {"tool": False, "edge_delete": True, "edge_add": True, "argument": True},
        "tool_edge_argument": {"tool": True, "edge_delete": True, "edge_add": True, "argument": True},
    }
    enabled = dict(mapping[args.mode])
    if args.disable_tool_critic:
        enabled["tool"] = False
    if args.disable_edge_critic:
        enabled["edge_delete"] = False
        enabled["edge_add"] = False
    if args.disable_argument_critic:
        enabled["argument"] = False
    enabled["edge"] = enabled["edge_delete"] or enabled["edge_add"]
    return enabled


def input_kind(args: argparse.Namespace) -> str:
    if args.input_json:
        return "json"
    if args.input_jsonl:
        return "jsonl"
    return "xlsx"


def resolve_input_path(args: argparse.Namespace) -> Path:
    return resolve_path(args.input_json or args.input_jsonl or args.input_xlsx)


def resolve_path(raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    return path.resolve() if path.is_absolute() else (REPO_ROOT / path).resolve()


def run_one_case(
    row: Dict[str, Any],
    case_id: str,
    client: OpenAICompatibleLLMClient,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    enabled: Dict[str, bool],
) -> Dict[str, Any]:
    warnings: List[str] = []
    user_request = get_first_value(row, ["user_request", "request", "instruction", "User Request"])
    original_result = extract_baseline_result(row, warnings)
    if original_result is None:
        return error_result(case_id, user_request, None, warnings + ["missing workflow result"])

    working_result = copy.deepcopy(original_result)
    task_nodes = normalize_task_nodes(working_result, warnings)
    original_edges = normalize_workflow_edges(working_result, task_nodes, warnings)
    current_edges = copy.deepcopy(original_edges)

    raw_tool = raw_delete = raw_add = raw_argument = ""
    tool_ops: List[Dict[str, Any]] = []
    rejected_tool_ops: List[Dict[str, Any]] = []
    remove_ops: List[Dict[str, Any]] = []
    add_ops: List[Dict[str, Any]] = []
    rejected_add_edges: List[Dict[str, Any]] = []
    argument_ops: List[Dict[str, Any]] = []

    if enabled.get("tool"):
        raw_tool, payload = call_json_critic(
            client,
            build_tool_critic_prompt(user_request, task_nodes, current_edges, tool_catalog, transition_index),
            warnings,
            "tool_critic",
        )
        tool_ops, rejected_tool_ops = apply_tool_operations(task_nodes, current_edges, payload, tool_catalog, warnings)

    if enabled.get("edge_delete"):
        raw_delete, payload = call_json_critic(
            client,
            build_edge_delete_prompt(user_request, task_nodes, current_edges, tool_catalog, transition_index),
            warnings,
            "edge_delete_critic",
        )
        remove_ops = apply_remove_edges(task_nodes, current_edges, payload, tool_catalog, warnings)
        current_edges = apply_edge_deletions(current_edges, remove_ops)

    if enabled.get("edge_add"):
        raw_add, payload = call_json_critic(
            client,
            build_edge_add_prompt(user_request, task_nodes, current_edges, tool_catalog, transition_index),
            warnings,
            "edge_add_critic",
        )
        add_ops, rejected_add_edges = apply_add_edges(task_nodes, current_edges, payload, tool_catalog, warnings)
        current_edges = apply_edge_additions(current_edges, add_ops)

    if enabled.get("argument"):
        raw_argument, payload = call_json_critic(
            client,
            build_argument_critic_prompt(user_request, task_nodes, current_edges, tool_catalog),
            warnings,
            "argument_critic",
        )
        argument_ops = normalize_argument_operations(payload)

    repaired_result = rebuild_result(working_result, task_nodes, current_edges, argument_ops, user_request)
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": repaired_result,
        "tool_operations": tool_ops,
        "edge_remove_operations": remove_ops,
        "edge_add_operations": add_ops,
        "argument_operations": argument_ops,
        "rejected_tool_operations": rejected_tool_ops,
        "rejected_add_edges": rejected_add_edges,
        "original_edges": original_edges,
        "final_edges": current_edges,
        "raw_tool_critic_output": raw_tool,
        "raw_edge_delete_output": raw_delete,
        "raw_edge_add_output": raw_add,
        "raw_argument_critic_output": raw_argument,
        "warnings": warnings,
    }


def error_result(case_id: str, user_request: str, original_result: Any, warnings: List[str]) -> Dict[str, Any]:
    return {
        "id": case_id,
        "user_request": user_request,
        "original_result": original_result,
        "repaired_result": original_result,
        "tool_operations": [],
        "edge_remove_operations": [],
        "edge_add_operations": [],
        "argument_operations": [],
        "rejected_tool_operations": [],
        "rejected_add_edges": [],
        "original_edges": [],
        "final_edges": [],
        "raw_tool_critic_output": "",
        "raw_edge_delete_output": "",
        "raw_edge_add_output": "",
        "raw_argument_critic_output": "",
        "warnings": warnings,
    }


def call_json_critic(
    client: OpenAICompatibleLLMClient,
    prompt: str,
    warnings: List[str],
    label: str,
) -> Tuple[str, Dict[str, Any]]:
    raw_text = ""
    try:
        raw_text = client.chat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        return raw_text, extract_json_object(raw_text)
    except Exception as exc:  # noqa: BLE001 - keep row-level output.
        warnings.append(f"{label}_error: {type(exc).__name__}: {exc}")
        return raw_text, {}


def build_tool_critic_prompt(
    user_request: str,
    task_nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> str:
    payload = {
        "user_request": user_request,
        "workflow_nodes": build_node_context(task_nodes, tool_catalog),
        "current_edges": enrich_edges(edges, task_nodes, tool_catalog, transition_index),
        "available_tools": list(tool_catalog.values()),
    }
    return f"""You are a Conservative Workflow Tool Critic.

You are reviewing a workflow generated by another model.

Your task is NOT to regenerate the workflow.
Your task is ONLY to detect obvious tool selection errors.

Default action: KEEP the current tool.

Only replace a tool when there is strong evidence that:
1. the current tool capability does not match the requested operation;
2. another available tool clearly matches the requested operation better;
3. replacing the tool preserves the intended input/output role of the node.

Do NOT replace a tool just because another tool is also plausible.
Do NOT optimize style.
Do NOT change node order.
Do NOT add or delete nodes.

Pay attention to tool semantic boundaries:
- search/retrieve existing content vs generate new content
- summarize vs expand
- paraphrase/rewrite vs simplify
- download existing media vs generate media
- extract from input vs search externally
- audio/video/image/text modality consistency

Return JSON only:
{{
  "tool_operations": [
    {{"node_index": 0, "op": "KEEP_TOOL", "old_tool": "...", "new_tool": null, "reason": "..."}},
    {{"node_index": 1, "op": "REPLACE_TOOL", "old_tool": "...", "new_tool": "...", "reason": "..."}}
  ],
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_edge_delete_prompt(
    user_request: str,
    task_nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> str:
    payload = {
        "user_request": user_request,
        "workflow_nodes": build_node_context(task_nodes, tool_catalog),
        "current_edges": enrich_edges(edges, task_nodes, tool_catalog, transition_index),
    }
    return f"""You are a Workflow Edge Validation Critic.

Your task is ONLY to identify existing edges that are obviously incorrect.
You MUST NOT add edges.
You MUST NOT redesign the workflow.
You MUST assume the workflow is mostly correct.

Default action: KEEP THE EDGE.

Valid reasons to remove an edge:
1. Type-incompatible edge. The source output type is clearly incompatible with the target input type.
2. Impossible dependency. The source cannot produce the artifact required by the target.
3. Obvious user request violation. The dependency clearly contradicts the requested workflow.

Invalid reasons:
Do NOT remove an edge because another predecessor appears more relevant, has higher transition
probability, is closer to the original input, would make the workflow shorter, or produces a cleaner artifact.

Return JSON only:
{{
  "remove_edges": [
    {{"source": 0, "target": 2, "reason": "..."}}
  ],
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_edge_add_prompt(
    user_request: str,
    task_nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> str:
    payload = {
        "user_request": user_request,
        "workflow_nodes": build_node_context(task_nodes, tool_catalog),
        "current_edges": enrich_edges(edges, task_nodes, tool_catalog, transition_index),
        "input_slot_status": build_input_slot_status(task_nodes, edges, tool_catalog),
        "candidate_edges": build_candidate_edges(task_nodes, edges, tool_catalog, transition_index),
    }
    return f"""You are a Workflow Input Slot Recovery Critic.

Your task is ONLY to recover missing REQUIRED inputs.
You MUST NOT optimize the workflow.
You MUST NOT redesign the workflow.
You MUST NOT add optional dependencies.

ADD_EDGE is much stricter than REMOVE_EDGE.
Only add an edge when a target node is missing a REQUIRED input.

Do NOT add edges merely because two nodes are semantically related, input/output types are compatible,
transition probability is high, source output could improve target quality, or source output is optional context.

For each target node:
1. determine required input slots from tool input-type;
2. determine currently satisfied slots from incoming <node-i> references and literal arguments;
3. identify missing required slots;
4. add edges only to fill missing required slots.

Chain preservation rule:
If target already has a valid predecessor and expected input count <= 1, DO NOT add another edge.
Do not bypass an existing processing chain.
Prefer preserving the latest transformed artifact.

Return JSON only:
{{
  "add_edges": [
    {{"source": 1, "target": 4, "missing_slot": "audio", "reason": "..."}}
  ],
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def build_argument_critic_prompt(
    user_request: str,
    task_nodes: List[Dict[str, Any]],
    final_edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> str:
    payload = {
        "user_request": user_request,
        "workflow_nodes": build_node_context(task_nodes, tool_catalog),
        "final_edges": final_edges,
    }
    return f"""You are a Conservative Workflow Argument Critic.

Do NOT change tools.
Do NOT change edges.
Do NOT add or delete nodes.

For each node, construct arguments that satisfy:
1. incoming edges should appear as <node-i> references;
2. user-provided files, URLs, quoted text, effect names, speed values, voice/style parameters should be preserved when required;
3. node references should appear before literal arguments;
4. do not add optional context arguments;
5. do not duplicate arguments.

Use final_edges as the source of truth for <node-i> references.

Return JSON only:
{{
  "argument_operations": [
    {{"node_index": 2, "old_arguments": [...], "new_arguments": [...], "reason": "..."}}
  ],
  "reason": "..."
}}

Input:
{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def extract_baseline_result(row: Dict[str, Any], warnings: List[str]) -> Dict[str, Any] | None:
    if "task_nodes" in row:
        return ensure_result_shape(parse_jsonish(row), warnings)
    for key in ["result", "qwen_result", "pre_result", "predicted_workflow", "predicted_result", "workflow"]:
        if key not in row:
            continue
        payload = parse_jsonish(row.get(key))
        if isinstance(payload, dict):
            if "task_nodes" in payload:
                return ensure_result_shape(payload, warnings)
            nested = parse_jsonish(payload.get("result"))
            if isinstance(nested, dict) and "task_nodes" in nested:
                return ensure_result_shape(nested, warnings)
    return None


def ensure_result_shape(payload: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    result = copy.deepcopy(payload)
    result["task_steps"] = parse_jsonish(result.get("task_steps"))
    result["task_nodes"] = parse_jsonish(result.get("task_nodes"))
    result["task_links"] = parse_jsonish(result.get("task_links"))
    if not isinstance(result.get("task_steps"), list):
        result["task_steps"] = []
    if not isinstance(result.get("task_nodes"), list):
        warnings.append("result.task_nodes is not a list; set to empty list")
        result["task_nodes"] = []
    if not isinstance(result.get("task_links"), list):
        result["task_links"] = []
    return result


def normalize_task_nodes(result: Dict[str, Any], warnings: List[str]) -> List[Dict[str, Any]]:
    raw_nodes = result.get("task_nodes")
    if not isinstance(raw_nodes, list):
        warnings.append("task_nodes missing or invalid")
        result["task_nodes"] = []
        return []
    nodes: List[Dict[str, Any]] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            warnings.append(f"task_nodes[{index}] is not an object; converted to empty node")
            node = {}
        item = copy.deepcopy(node)
        item["task"] = str(item.get("task") or "").strip()
        if not isinstance(item.get("arguments"), list):
            item["arguments"] = [] if item.get("arguments") is None else [item.get("arguments")]
        nodes.append(item)
    result["task_nodes"] = nodes
    return nodes


def normalize_workflow_edges(result: Dict[str, Any], nodes: List[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, int]]:
    arg_edges = extract_edges_from_arguments(nodes, warnings)
    link_edges = infer_edges_from_task_links(result.get("task_links", []), nodes)
    if arg_edges and edge_key_set(arg_edges) != edge_key_set(link_edges):
        warnings.append("task_links differ from <node-i> arguments; arguments used as edge source of truth")
    if arg_edges:
        return arg_edges
    if link_edges:
        warnings.append("no <node-i> argument edges found; inferred edges from task_links")
        return link_edges
    return []


def extract_edges_from_arguments(nodes: List[Dict[str, Any]], warnings: List[str]) -> List[Dict[str, int]]:
    edges: set[Tuple[int, int]] = set()
    for target, node in enumerate(nodes):
        for argument in node.get("arguments", []) if isinstance(node, dict) else []:
            source = parse_node_ref(argument)
            if source is None:
                continue
            if source < 0 or source >= len(nodes):
                warnings.append(f"ignore invalid edge {source}->{target}: source out of range")
                continue
            if source >= target:
                warnings.append(f"ignore invalid edge {source}->{target}: source must be < target")
                continue
            edges.add((source, target))
    return edge_tuples_to_dicts(sorted(edges, key=lambda item: (item[1], item[0])))


def infer_edges_from_task_links(task_links: Any, nodes: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    if not isinstance(task_links, list):
        return []
    names = [normalize_tool_key(node.get("task", "")) for node in nodes]
    edges: set[Tuple[int, int]] = set()
    for link in task_links:
        if not isinstance(link, dict):
            continue
        source_name = normalize_tool_key(link.get("source", ""))
        target_name = normalize_tool_key(link.get("target", ""))
        for target, name in enumerate(names):
            if name != target_name:
                continue
            candidates = [index for index, value in enumerate(names[:target]) if value == source_name]
            if candidates:
                edges.add((candidates[-1], target))
                break
    return edge_tuples_to_dicts(sorted(edges, key=lambda item: (item[1], item[0])))


def apply_tool_operations(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    payload: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    warnings: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    raw_ops = payload.get("tool_operations", [])
    if not isinstance(raw_ops, list):
        warnings.append("tool_operations is not a list; ignored")
        return [], []
    applied: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_ops):
        if not isinstance(item, dict):
            warnings.append(f"tool_operations[{index}] is not an object; ignored")
            continue
        node_index = coerce_int(item.get("node_index"))
        op = str(item.get("op") or "KEEP_TOOL").strip().upper()
        reason = str(item.get("reason") or "")
        if node_index is None or node_index < 0 or node_index >= len(nodes):
            rejected.append({**item, "reject_reason": "invalid node_index"})
            continue
        current_tool = str(nodes[node_index].get("task") or "")
        if op != "REPLACE_TOOL":
            applied.append(
                {
                    "node_index": node_index,
                    "op": "KEEP_TOOL",
                    "old_tool": current_tool,
                    "new_tool": None,
                    "reason": reason,
                }
            )
            continue
        old_tool = str(item.get("old_tool") or "")
        new_tool = str(item.get("new_tool") or "")
        if normalize_tool_key(old_tool) not in {"", normalize_tool_key(current_tool)}:
            rejected.append({**item, "reject_reason": "old_tool does not match current node"})
            continue
        if normalize_tool_key(new_tool) not in tool_catalog:
            rejected.append({**item, "reject_reason": "new_tool not found in tool_desc"})
            continue
        if not is_high_confidence_reason(reason):
            rejected.append({**item, "reject_reason": "reason is not high-confidence"})
            continue
        new_tool_record = lookup_tool(tool_catalog, new_tool)
        if not replacement_role_compatible(node_index, new_tool_record, nodes, edges, tool_catalog):
            rejected.append({**item, "reject_reason": "replacement breaks predecessor/successor modality"})
            continue
        nodes[node_index]["task"] = new_tool_record["id"]
        applied.append(
            {
                "node_index": node_index,
                "op": "REPLACE_TOOL",
                "old_tool": current_tool,
                "new_tool": new_tool_record["id"],
                "reason": reason,
            }
        )
    return applied, rejected


def replacement_role_compatible(
    node_index: int,
    new_tool: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> bool:
    new_input = new_tool.get("input_types", [])
    new_output = new_tool.get("output_types", [])
    for edge in edges:
        if edge["target"] == node_index:
            source_tool = lookup_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
            if not tool_is_known(source_tool):
                return False
            if type_compatible(source_tool.get("output_types", []), new_input) is not True:
                return False
        if edge["source"] == node_index:
            target_tool = lookup_tool(tool_catalog, nodes[edge["target"]].get("task", ""))
            if not tool_is_known(target_tool):
                return False
            if type_compatible(new_output, target_tool.get("input_types", [])) is not True:
                return False
    return True


def is_high_confidence_reason(reason: str) -> bool:
    text = str(reason or "").lower()
    if any(token in text for token in ["also plausible", "could", "maybe", "might", "style", "optimize"]):
        return False
    return any(
        token in text
        for token in [
            "does not match",
            "cannot",
            "wrong",
            "incompatible",
            "clearly",
            "capability",
            "modality",
            "instead",
        ]
    )


def apply_remove_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    payload: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    normalized = normalize_edge_operations(payload.get("remove_edges", []), "REMOVE_EDGE", warnings, "remove_edges")
    current = edge_key_set(edges)
    applied: List[Dict[str, Any]] = []
    for edge in normalized:
        source = edge["source"]
        target = edge["target"]
        if not is_valid_edge_tuple(source, target, len(nodes)):
            warnings.append(f"remove_edges {source}->{target} invalid; ignored")
            continue
        if (source, target) not in current:
            warnings.append(f"remove_edges {source}->{target} not in current edges; ignored")
            continue
        if not is_safe_remove_edge(edge, nodes, tool_catalog):
            reason = edge.get("reject_reason") or "conservative gate"
            warnings.append(f"remove_edges {source}->{target} rejected by {reason}")
            continue
        applied.append(edge)
    return applied


def is_safe_remove_edge(edge: Dict[str, Any], nodes: List[Dict[str, Any]], tool_catalog: Dict[str, Dict[str, Any]]) -> bool:
    source_tool = lookup_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
    target_tool = lookup_tool(tool_catalog, nodes[edge["target"]].get("task", ""))
    if not tool_is_known(source_tool) or not tool_is_known(target_tool):
        edge["reject_reason"] = "unknown tool; keep baseline edge"
        return False
    compatibility = type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", []))
    if compatibility is False:
        edge["gate_reason"] = "type_incompatible"
        return True
    reason = str(edge.get("reason") or "").lower()
    if any(token in reason for token in ["impossible", "contradict", "violation", "cannot produce", "wrong artifact"]):
        edge["reject_reason"] = "strong_reason disabled; keep baseline edge"
    return False


def apply_add_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    payload: Dict[str, Any],
    tool_catalog: Dict[str, Dict[str, Any]],
    warnings: List[str],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    normalized = normalize_edge_operations(payload.get("add_edges", []), "ADD_EDGE", warnings, "add_edges")
    current_edges = copy.deepcopy(edges)
    applied: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for edge in normalized:
        reject_reason = validate_add_edge(edge, nodes, current_edges, tool_catalog)
        if reject_reason:
            rejected.append({**edge, "reject_reason": reject_reason})
            continue
        applied.append(edge)
        current_edges.append({"source": edge["source"], "target": edge["target"]})
    return applied, rejected


def validate_add_edge(
    edge: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> str:
    source = edge["source"]
    target = edge["target"]
    if not is_valid_edge_tuple(source, target, len(nodes)):
        return "invalid source/target"
    if (source, target) in edge_key_set(edges):
        return "duplicate edge"
    source_tool = lookup_tool(tool_catalog, nodes[source].get("task", ""))
    target_tool = lookup_tool(tool_catalog, nodes[target].get("task", ""))
    if not tool_is_known(source_tool) or not tool_is_known(target_tool):
        return "unknown source/target tool"
    if type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", [])) is not True:
        return "type_compatible=false"
    required_slots = normalize_type_list(target_tool.get("input_types", []))
    if not required_slots:
        return "target has no declared required inputs"
    status = input_slot_status_for_target(target, nodes, edges, tool_catalog)
    if len(required_slots) <= 1 and status["incoming_count"] >= 1:
        return "single-input target already has valid predecessor"
    if not status["missing_slots"]:
        return "target already has enough literal + node arguments"
    source_types = normalize_type_set(source_tool.get("output_types", []))
    missing_slot = normalize_type_name(edge.get("missing_slot") or "")
    if missing_slot and missing_slot not in status["missing_slots"]:
        return "missing_slot is not actually missing"
    if missing_slot and missing_slot not in source_types:
        return "source output does not match missing_slot"
    if not source_types.intersection(status["missing_slots"]):
        return "source output does not fill any missing slot"
    edge["gate_reason"] = "fills_required_input_slot"
    return ""


def input_slot_status_for_target(
    target: int,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    target_tool = lookup_tool(tool_catalog, nodes[target].get("task", ""))
    remaining = normalize_type_list(target_tool.get("input_types", []))
    satisfied: List[Dict[str, Any]] = []
    incoming_count = 0
    for edge in edges:
        if edge.get("target") != target:
            continue
        source_tool = lookup_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
        matched = consume_first_matching_slot(remaining, source_tool.get("output_types", []))
        if matched:
            incoming_count += 1
            satisfied.append({"kind": "node", "source": edge["source"], "slot": matched})
    for argument in nodes[target].get("arguments", []):
        if parse_node_ref(argument) is not None:
            continue
        matched = consume_first_matching_slot(remaining, infer_literal_argument_types(argument))
        if matched:
            satisfied.append({"kind": "literal", "value": argument, "slot": matched})
    return {
        "target": target,
        "required_slots": normalize_type_list(target_tool.get("input_types", [])),
        "satisfied_slots": satisfied,
        "missing_slots": list(remaining),
        "incoming_count": incoming_count,
    }


def build_input_slot_status(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    return [input_slot_status_for_target(index, nodes, edges, tool_catalog) for index in range(len(nodes))]


def consume_first_matching_slot(remaining: List[str], candidate_types: Any) -> str:
    candidates = normalize_type_set(candidate_types)
    for index, slot in enumerate(list(remaining)):
        if slot in candidates or "any" in candidates or "*" in candidates:
            remaining.pop(index)
            return slot
    return ""


def normalize_argument_operations(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_ops = payload.get("argument_operations", [])
    if not isinstance(raw_ops, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in raw_ops:
        if not isinstance(item, dict):
            continue
        node_index = coerce_int(item.get("node_index"))
        if node_index is None:
            continue
        normalized.append(
            {
                "node_index": node_index,
                "old_arguments": coerce_list(item.get("old_arguments", [])),
                "new_arguments": coerce_list(item.get("new_arguments", [])),
                "reason": str(item.get("reason") or ""),
            }
        )
    return normalized


def rebuild_result(
    original_result: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    final_edges: List[Dict[str, int]],
    argument_operations: List[Dict[str, Any]],
    user_request: str,
) -> Dict[str, Any]:
    result = copy.deepcopy(original_result)
    repaired_nodes = copy.deepcopy(nodes)
    incoming_by_target: Dict[int, List[int]] = {}
    for edge in final_edges:
        incoming_by_target.setdefault(edge["target"], []).append(edge["source"])
    proposed_literals = build_proposed_literal_index(argument_operations, user_request, repaired_nodes)
    for index, node in enumerate(repaired_nodes):
        old_arguments = node.get("arguments", [])
        if not isinstance(old_arguments, list):
            old_arguments = [] if old_arguments is None else [old_arguments]
        refs = [f"<node-{source}>" for source in sorted(set(incoming_by_target.get(index, [])))]
        literals = [argument for argument in old_arguments if parse_node_ref(argument) is None]
        literals.extend(proposed_literals.get(index, []))
        node["arguments"] = dedupe_preserve_order(refs + literals)
    result["task_steps"] = copy.deepcopy(original_result.get("task_steps", []))
    result["task_nodes"] = repaired_nodes
    result["task_links"] = build_task_links(repaired_nodes, final_edges)
    return result


def build_proposed_literal_index(
    argument_operations: List[Dict[str, Any]],
    user_request: str,
    nodes: List[Dict[str, Any]],
) -> Dict[int, List[Any]]:
    original_literals = {
        str(argument).strip()
        for node in nodes
        for argument in node.get("arguments", [])
        if parse_node_ref(argument) is None and str(argument).strip()
    }
    proposed: Dict[int, List[Any]] = {}
    request_text = str(user_request or "")
    for operation in argument_operations:
        node_index = operation.get("node_index")
        if not isinstance(node_index, int) or node_index < 0 or node_index >= len(nodes):
            continue
        for argument in coerce_list(operation.get("new_arguments", [])):
            if parse_node_ref(argument) is not None:
                continue
            text = str(argument or "").strip()
            if text and (text in original_literals or text in request_text):
                proposed.setdefault(node_index, []).append(argument)
    return proposed


def build_node_context(nodes: List[Dict[str, Any]], tool_catalog: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    context: List[Dict[str, Any]] = []
    for index, node in enumerate(nodes):
        tool = lookup_tool(tool_catalog, node.get("task", ""))
        context.append(
            {
                "index": index,
                "task": str(node.get("task") or ""),
                "arguments": copy.deepcopy(node.get("arguments", [])),
                "tool_desc": tool.get("desc", ""),
                "input_types": list(tool.get("input_types", [])),
                "output_types": list(tool.get("output_types", [])),
                "intent": tool.get("intent", ""),
            }
        )
    return context


def enrich_edges(
    edges: List[Dict[str, int]],
    nodes: List[Dict[str, Any]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> List[Dict[str, Any]]:
    enriched: List[Dict[str, Any]] = []
    for edge in edges:
        source_tool = lookup_tool(tool_catalog, nodes[edge["source"]].get("task", ""))
        target_tool = lookup_tool(tool_catalog, nodes[edge["target"]].get("task", ""))
        enriched.append(
            {
                "source": edge["source"],
                "target": edge["target"],
                "source_tool": source_tool.get("id", ""),
                "target_tool": target_tool.get("id", ""),
                "source_output_types": source_tool.get("output_types", []),
                "target_input_types": target_tool.get("input_types", []),
                "type_compatible": type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", [])),
                "transition_probability": get_transition_probability(
                    transition_index,
                    source_tool.get("id", ""),
                    target_tool.get("id", ""),
                ),
            }
        )
    return enriched


def build_candidate_edges(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> List[Dict[str, Any]]:
    existing = edge_key_set(edges)
    candidates: List[Dict[str, Any]] = []
    for target in range(len(nodes)):
        status = input_slot_status_for_target(target, nodes, edges, tool_catalog)
        if not status["missing_slots"]:
            continue
        target_tool = lookup_tool(tool_catalog, nodes[target].get("task", ""))
        for source in range(target):
            if (source, target) in existing:
                continue
            source_tool = lookup_tool(tool_catalog, nodes[source].get("task", ""))
            if type_compatible(source_tool.get("output_types", []), target_tool.get("input_types", [])) is not True:
                continue
            candidates.append(
                {
                    "source": source,
                    "target": target,
                    "source_tool": source_tool.get("id", ""),
                    "target_tool": target_tool.get("id", ""),
                    "source_output_types": source_tool.get("output_types", []),
                    "missing_slots": status["missing_slots"],
                    "transition_probability": get_transition_probability(
                        transition_index,
                        source_tool.get("id", ""),
                        target_tool.get("id", ""),
                    ),
                }
            )
    return candidates


def load_tool_catalog(path: Path) -> Dict[str, Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    nodes = payload.get("nodes") if isinstance(payload, dict) else payload
    if isinstance(nodes, dict):
        items = list(nodes.values())
    elif isinstance(nodes, list):
        items = nodes
    else:
        raise ValueError(f"tool desc nodes must be a list or object: {path}")
    catalog: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        tool_id = str(item.get("id") or item.get("tool_id") or item.get("name") or "").strip()
        if not tool_id:
            continue
        desc = str(item.get("desc") or item.get("description") or "")
        intent = str(item.get("intent") or "").strip() or infer_intent(tool_id, desc)
        catalog[normalize_tool_key(tool_id)] = {
            "id": tool_id,
            "desc": desc,
            "input_types": coerce_str_list(item.get("input-type") or item.get("input_type") or item.get("input_types")),
            "output_types": coerce_str_list(item.get("output-type") or item.get("output_type") or item.get("output_types")),
            "intent": intent,
            "known": True,
        }
    return catalog


def infer_intent(tool_id: str, desc: str) -> str:
    text = f"{desc} {tool_id}".lower()
    candidates = [
        "download",
        "search",
        "generate",
        "extract",
        "summarize",
        "expand",
        "paraphrase",
        "rewrite",
        "simplify",
        "translate",
        "analyze",
        "convert",
        "stabilize",
        "synchronize",
        "splice",
        "colorize",
        "voiceover",
        "change",
        "apply",
        "reduce",
    ]
    for candidate in candidates:
        if candidate in text:
            return candidate
    words = re.sub(r"[^a-z-]", " ", text).split()
    return normalize_verb(words[0]) if words else "unknown"


def normalize_verb(word: str) -> str:
    text = str(word or "").lower().strip()
    replacements = {
        "extracts": "extract",
        "downloads": "download",
        "generates": "generate",
        "summarizes": "summarize",
        "translates": "translate",
        "converts": "convert",
        "applies": "apply",
        "reduces": "reduce",
    }
    if text in replacements:
        return replacements[text]
    if text.endswith("ing") and len(text) > 5:
        return text[:-3]
    if text.endswith("s") and len(text) > 3:
        return text[:-1]
    return text or "unknown"


def lookup_tool(tool_catalog: Dict[str, Dict[str, Any]], task_name: Any) -> Dict[str, Any]:
    key = normalize_tool_key(task_name)
    if key in tool_catalog:
        return tool_catalog[key]
    stripped = re.sub(r"\s*\(.*\)\s*$", "", str(task_name)).strip()
    key = normalize_tool_key(stripped)
    if key in tool_catalog:
        return tool_catalog[key]
    return {"id": str(task_name or ""), "desc": "", "input_types": [], "output_types": [], "intent": "unknown", "known": False}


def tool_is_known(tool: Dict[str, Any]) -> bool:
    return bool(tool.get("known", False))


def load_transition_index(raw_path: Any) -> Dict[Tuple[str, str], Any]:
    if not raw_path:
        return {}
    path = resolve_path(raw_path)
    if not path.exists():
        raise FileNotFoundError(f"transition graph not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    index: Dict[Tuple[str, str], Any] = {}
    if not isinstance(payload, dict):
        return index
    for edge in coerce_list(payload.get("edges")) + coerce_list(payload.get("links")):
        if isinstance(edge, dict):
            add_transition_edge(index, edge)
    adjacency = payload.get("adjacency")
    if isinstance(adjacency, dict):
        for source, raw_targets in adjacency.items():
            if isinstance(raw_targets, dict):
                for target, value in raw_targets.items():
                    index[(normalize_tool_key(source), normalize_tool_key(target))] = extract_probability(value)
            elif isinstance(raw_targets, list):
                for item in raw_targets:
                    if not isinstance(item, dict):
                        continue
                    target = item.get("target_tool_id") or item.get("target") or item.get("target_tool")
                    if target:
                        index[(normalize_tool_key(source), normalize_tool_key(target))] = extract_probability(item)
    return index


def add_transition_edge(index: Dict[Tuple[str, str], Any], edge: Dict[str, Any]) -> None:
    source = edge.get("source_tool_id") or edge.get("source") or edge.get("from")
    target = edge.get("target_tool_id") or edge.get("target") or edge.get("to")
    if source and target:
        index[(normalize_tool_key(source), normalize_tool_key(target))] = extract_probability(edge)


def extract_probability(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, dict):
        return None
    for key in ["transition_probability", "probability", "prob", "weight", "score"]:
        number = coerce_float(value.get(key))
        if number is not None:
            return number
    return None


def get_transition_probability(index: Dict[Tuple[str, str], Any], source_tool: Any, target_tool: Any) -> Any:
    if not index:
        return None
    return index.get((normalize_tool_key(source_tool), normalize_tool_key(target_tool)))


def normalize_edge_operations(raw_edges: Any, op_name: str, warnings: List[str], source_label: str) -> List[Dict[str, Any]]:
    if raw_edges is None:
        return []
    if not isinstance(raw_edges, list):
        warnings.append(f"{source_label} is not a list; ignored")
        return []
    normalized: List[Dict[str, Any]] = []
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            warnings.append(f"{source_label}[{index}] is not an object; ignored")
            continue
        source = coerce_int(item.get("source"))
        target = coerce_int(item.get("target"))
        if source is None or target is None:
            warnings.append(f"{source_label}[{index}] missing int source/target; ignored")
            continue
        normalized.append(
            {
                "op": op_name,
                "source": source,
                "target": target,
                "missing_slot": str(item.get("missing_slot") or ""),
                "reason": str(item.get("reason") or ""),
            }
        )
    return normalized


def apply_edge_deletions(edges: List[Dict[str, int]], remove_edges: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    edge_set = edge_key_set(edges)
    for edge in remove_edges:
        edge_set.discard((edge["source"], edge["target"]))
    return edge_tuples_to_dicts(sorted(edge_set, key=lambda item: (item[1], item[0])))


def apply_edge_additions(edges: List[Dict[str, int]], add_edges: List[Dict[str, Any]]) -> List[Dict[str, int]]:
    edge_set = edge_key_set(edges)
    for edge in add_edges:
        edge_set.add((edge["source"], edge["target"]))
    return edge_tuples_to_dicts(sorted(edge_set, key=lambda item: (item[1], item[0])))


def build_task_links(nodes: List[Dict[str, Any]], edges: List[Dict[str, int]]) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if 0 <= source < len(nodes) and 0 <= target < len(nodes):
            links.append({"source": str(nodes[source].get("task") or ""), "target": str(nodes[target].get("task") or "")})
    return links


def is_valid_edge_tuple(source: int, target: int, node_count: int) -> bool:
    return 0 <= source < node_count and 0 <= target < node_count and source < target


def edge_key_set(edges: List[Dict[str, int]]) -> set[Tuple[int, int]]:
    return {(int(edge["source"]), int(edge["target"])) for edge in edges}


def edge_tuples_to_dicts(edges: Iterable[Tuple[int, int]]) -> List[Dict[str, int]]:
    return [{"source": source, "target": target} for source, target in edges]


def parse_node_ref(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = NODE_REF_RE.fullmatch(value.strip())
    return int(match.group(1)) if match else None


def type_compatible(source_output: Any, target_input: Any) -> bool | str:
    target_types = normalize_type_set(target_input)
    if not target_types:
        return "unknown"
    source_types = normalize_type_set(source_output)
    if not source_types:
        return "unknown"
    if "any" in target_types or "*" in target_types:
        return True
    return bool(source_types & target_types)


def normalize_type_set(value: Any) -> set[str]:
    return {normalize_type_name(item) for item in coerce_list(value) if normalize_type_name(item)}


def normalize_type_list(value: Any) -> List[str]:
    return [normalize_type_name(item) for item in coerce_list(value) if normalize_type_name(item)]


def normalize_type_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


def infer_literal_argument_type(argument: Any) -> str:
    types = infer_literal_argument_types(argument)
    return types[0] if types else ""


def infer_literal_argument_types(argument: Any) -> List[str]:
    text = str(argument or "").strip().lower()
    if not text:
        return []
    inferred: List[str] = []
    if any(f".{ext}" in text for ext in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "svg", "ico"]):
        inferred.append("image")
    elif any(f".{ext}" in text for ext in ["mp3", "wav", "wma", "ogg", "aac", "flac", "aiff", "au"]):
        inferred.append("audio")
    elif any(f".{ext}" in text for ext in ["mp4", "avi", "mov", "flv", "wmv", "mkv", "webm", "m4v", "mpg", "mpeg"]):
        inferred.append("video")
    if re.match(r"^[a-z][a-z0-9+.-]*://", text):
        inferred.append("url")
    if not inferred:
        inferred.append("text")
    return dedupe_preserve_order(inferred)


def read_input_records(path: Path, kind: str) -> List[Dict[str, Any]]:
    if kind == "jsonl":
        return read_jsonl_records(path)
    if kind == "xlsx":
        return read_xlsx_records(path)
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ["records", "data", "rows", "items"]:
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        return [payload]
    raise ValueError(f"JSON input must be object or list: {path}")


def read_jsonl_records(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                print(f"[WARN] skip invalid JSONL line {line_number}: {exc}")
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_completed_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    completed = set()
    for row in read_jsonl_records(path):
        case_id = str(row.get("id") or row.get("ID") or "").strip()
        if case_id:
            completed.add(case_id)
    return completed


def read_xlsx_records(path: Path) -> List[Dict[str, Any]]:
    rows = read_xlsx_rows(path)
    if not rows:
        return []
    header = rows[0]
    records: List[Dict[str, Any]] = []
    for row in rows[1:]:
        record = {header[index]: row[index] if index < len(row) else "" for index in range(len(header))}
        if any(str(value or "").strip() for value in record.values()):
            records.append(record)
    return records


def read_xlsx_rows(path: Path) -> List[List[str]]:
    with zipfile.ZipFile(path) as archive:
        shared_strings = read_shared_strings(archive)
        sheet_name = first_worksheet_name(archive)
        sheet = ET.fromstring(archive.read(sheet_name))

    rows: List[List[str]] = []
    for row in sheet.findall(".//a:sheetData/a:row", XML_NS):
        values: List[str] = []
        for cell in row.findall("a:c", XML_NS):
            index = excel_column_index(cell.attrib.get("r", "A1"))
            while len(values) <= index:
                values.append("")
            values[index] = read_cell_text(cell, shared_strings)
        rows.append(values)
    return rows


def first_worksheet_name(archive: zipfile.ZipFile) -> str:
    for name in archive.namelist():
        if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
            return name
    raise ValueError("xlsx has no worksheet xml")


def read_shared_strings(archive: zipfile.ZipFile) -> List[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    shared: List[str] = []
    for item in root.findall("a:si", XML_NS):
        shared.append("".join(text.text or "" for text in item.findall(".//a:t", XML_NS)))
    return shared


def read_cell_text(cell: ET.Element, shared_strings: List[str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "s":
        value = cell.find("a:v", XML_NS)
        if value is None or value.text is None:
            return ""
        index = coerce_int(value.text)
        if index is None or index < 0 or index >= len(shared_strings):
            return ""
        return shared_strings[index]
    if cell_type == "inlineStr":
        return "".join(text.text or "" for text in cell.findall(".//a:t", XML_NS))
    value = cell.find("a:v", XML_NS)
    return value.text if value is not None and value.text is not None else ""


def excel_column_index(cell_ref: str) -> int:
    letters = "".join(char for char in str(cell_ref) if char.isalpha())
    result = 0
    for char in letters:
        result = result * 26 + ord(char.upper()) - 64
    return max(result - 1, 0)


def write_xlsx(path: Path, rows: List[Dict[str, Any]]) -> None:
    xlsx_rows: List[List[Any]] = [XLSX_HEADERS]
    for row in rows:
        xlsx_rows.append([xlsx_value(row.get(header, "")) for header in XLSX_HEADERS])
    write_xlsx_rows(path, xlsx_rows)


def xlsx_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value


def write_xlsx_rows(path: Path, rows: List[List[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    width_count = max(len(XLSX_HEADERS), max((len(row) for row in rows), default=0), 1)
    dim = f"A1:{column_name(width_count - 1)}{max(len(rows), 1)}"
    widths = [18, 70, 42, 42, 42, 42, 42, 42, 32, 32, 52, 90, 90, 90, 90, 90, 90]
    if len(widths) < width_count:
        widths.extend([32] * (width_count - len(widths)))
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths[:width_count], start=1)
    )

    row_xml: List[str] = []
    for row_index, row in enumerate(rows, start=1):
        style = 1 if row_index == 1 else 2
        height = 24 if row_index == 1 else 90
        cells = "".join(
            cell_xml(value, row_index, col_index, style)
            for col_index, value in enumerate(row)
        )
        row_xml.append(f'<row r="{row_index}" ht="{height}" customHeight="1">{cells}</row>')

    sheet_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <dimension ref="{dim}"/>
  <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
  <cols>{cols}</cols>
  <sheetData>{''.join(row_xml)}</sheetData>
  <autoFilter ref="{dim}"/>
</worksheet>'''
    write_minimal_xlsx_package(path, sheet_xml)


def cell_xml(value: Any, row_index: int, col_index: int, style: int) -> str:
    ref = f"{column_name(col_index)}{row_index}"
    text = "" if value is None else str(value)
    text = "".join(char for char in text if char in "\t\n\r" or ord(char) >= 32)
    escaped = html.escape(text, quote=False)
    return (
        f'<c r="{ref}" t="inlineStr" s="{style}">'
        f'<is><t xml:space="preserve">{escaped}</t></is></c>'
    )


def column_name(index: int) -> str:
    result = ""
    index += 1
    while index:
        index, remainder = divmod(index - 1, 26)
        result = chr(65 + remainder) + result
    return result


def write_minimal_xlsx_package(path: Path, sheet_xml: str) -> None:
    styles_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>
  <fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
  <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="3">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment wrapText="1" vertical="top"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
  <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    workbook_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="baseline_critic" sheetId="1" r:id="rId1"/></sheets>
</workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>'''
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    core_xml = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:creator>MIWP</dc:creator><cp:lastModifiedBy>MIWP</cp:lastModifiedBy><dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created><dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>
</cp:coreProperties>'''
    app_xml = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">
  <Application>Python stdlib</Application>
</Properties>'''
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types.encode("utf-8"))
        archive.writestr("_rels/.rels", root_rels.encode("utf-8"))
        archive.writestr("xl/workbook.xml", workbook_xml.encode("utf-8"))
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels.encode("utf-8"))
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml.encode("utf-8"))
        archive.writestr("xl/styles.xml", styles_xml.encode("utf-8"))
        archive.writestr("docProps/core.xml", core_xml.encode("utf-8"))
        archive.writestr("docProps/app.xml", app_xml.encode("utf-8"))


def parse_jsonish(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        return value


def get_case_id(row: Dict[str, Any], row_index: int) -> str:
    value = get_first_value(row, ["id", "ID", "case_id", "CaseID", "caseId"])
    return value if value else f"row_{row_index}"


def get_first_value(row: Dict[str, Any], keys: List[str]) -> str:
    for key in keys:
        if key in row and str(row.get(key) or "").strip():
            return str(row.get(key)).strip()
    lowered = {str(key).lower(): value for key, value in row.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if str(value or "").strip():
            return str(value).strip()
    return ""


def normalize_tool_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def coerce_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def coerce_str_list(value: Any) -> List[str]:
    return [str(item).strip() for item in coerce_list(value) if str(item).strip()]


def coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    text = str(value or "").strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return None


def coerce_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def dedupe_preserve_order(values: Iterable[Any]) -> List[Any]:
    seen: set[str] = set()
    result: List[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
