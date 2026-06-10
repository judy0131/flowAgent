# -*- coding: utf-8 -*-
"""Offline Qwen workflow edge refinement experiment.

This script validates whether a Qwen-generated workflow can improve DAG edge F1
after an LLM critic refines only dependency edges. It does not integrate with
the MIWP runtime and does not change task understanding, planning, or baseline
generation code.
"""

from __future__ import annotations

import argparse
import ast
import copy
import html
import json
import re
import sys
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
DEFAULT_OUTPUT_JSONL = (
    "agent/memory_guided_workflow/outputs/qwen_workflow_edge_refined.jsonl"
)
DEFAULT_OUTPUT_XLSX = (
    "agent/memory_guided_workflow/outputs/qwen_workflow_edge_refined.xlsx"
)

NODE_REF_RE = re.compile(r"^<node-(\d+)>$")
XML_NS = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
EDGE_REFINE_MODES = ("remove_only", "remove_plus_merge_add", "two_stage")

XLSX_HEADERS = [
    "ID",
    "edge_refine_mode",
    "user_request",
    "original_edges",
    "stage1_removed_edges",
    "stage2_added_edges",
    "final_edges",
    "edge_change_count",
    "warnings",
    "original_result",
    "refined_result",
    "raw_llm_output",
]


def main() -> int:
    args = parse_args()
    input_path = resolve_input_path(args)
    rows = read_input_records(input_path, is_xlsx=bool(args.input_xlsx))
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]

    output_jsonl = resolve_path(args.output_jsonl)
    output_xlsx = resolve_path(args.output_xlsx)
    tool_catalog = load_tool_catalog(resolve_path(args.tool_desc))
    transition_index = load_transition_index(args.transition_graph)

    result_by_id = load_existing_results(output_jsonl) if args.resume else {}
    if args.workers <= 1:
        client = OpenAICompatibleLLMClient(
            llm_config_path=args.llm_config,
            llm_profile=args.llm_profile,
        )
        for row_index, row in enumerate(rows, start=1):
            case_id = get_case_id(row, row_index)
            if args.resume and case_id in result_by_id:
                print(f"[{row_index}/{len(rows)}] skip ID={case_id} (resume)")
                continue

            print(f"[{row_index}/{len(rows)}] refine ID={case_id}")
            result = run_one_case(
                row=row,
                case_id=case_id,
                client=client,
                tool_catalog=tool_catalog,
                transition_index=transition_index,
                edge_refine_mode=args.edge_refine_mode,
            )
            append_jsonl(output_jsonl, result)
            result_by_id[case_id] = result
    else:
        futures = {}
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            for row_index, row in enumerate(rows, start=1):
                case_id = get_case_id(row, row_index)
                if args.resume and case_id in result_by_id:
                    print(f"[{row_index}/{len(rows)}] skip ID={case_id} (resume)")
                    continue
                print(f"[{row_index}/{len(rows)}] submit ID={case_id}")
                future = executor.submit(
                    run_one_case_with_new_client,
                    row=row,
                    case_id=case_id,
                    llm_config=args.llm_config,
                    llm_profile=args.llm_profile,
                    tool_catalog=tool_catalog,
                    transition_index=transition_index,
                    edge_refine_mode=args.edge_refine_mode,
                )
                futures[future] = (row_index, case_id)

            for future in as_completed(futures):
                row_index, case_id = futures[future]
                result = future.result()
                append_jsonl(output_jsonl, result)
                result_by_id[case_id] = result
                print(f"[{row_index}/{len(rows)}] done ID={case_id}")

    ordered_results = [
        result_by_id[get_case_id(row, index)]
        for index, row in enumerate(rows, start=1)
        if get_case_id(row, index) in result_by_id
    ]
    write_jsonl(output_jsonl, ordered_results)
    write_xlsx(output_xlsx, ordered_results)
    print(f"[DONE] jsonl={output_jsonl}")
    print(f"[DONE] xlsx={output_xlsx}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline Qwen workflow edge refinement experiment."
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input-xlsx", default=None)
    input_group.add_argument("--input-jsonl", default=None)
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--transition-graph", default=None)
    parser.add_argument("--output-jsonl", default=DEFAULT_OUTPUT_JSONL)
    parser.add_argument("--output-xlsx", default=DEFAULT_OUTPUT_XLSX)
    parser.add_argument("--llm-config", default="configs/qwen.json")
    parser.add_argument("--llm-profile", default=None)
    parser.add_argument(
        "--edge-refine-mode",
        choices=EDGE_REFINE_MODES,
        default="two_stage",
        help=(
            "Edge refinement ablation mode: remove_only, "
            "remove_plus_merge_add, or two_stage."
        ),
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of concurrent LLM workers. Use 1 for sequential execution.",
    )
    return parser.parse_args()


def resolve_input_path(args: argparse.Namespace) -> Path:
    raw = args.input_xlsx or args.input_jsonl
    return resolve_path(raw)


def resolve_path(raw: Any) -> Path:
    path = Path(str(raw)).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (REPO_ROOT / path).resolve()


def read_input_records(path: Path, is_xlsx: bool) -> List[Dict[str, Any]]:
    if is_xlsx:
        return read_xlsx_records(path)
    return read_jsonl_records(path)


def run_one_case(
    row: Dict[str, Any],
    case_id: str,
    client: OpenAICompatibleLLMClient,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    edge_refine_mode: str,
) -> Dict[str, Any]:
    warnings: List[str] = []
    user_request = get_first_value(row, ["pre_request", "user_request", "request", "instruction"])
    qwen_result = extract_qwen_result(row, warnings)
    if qwen_result is None:
        return build_error_result(
            case_id=case_id,
            edge_refine_mode=edge_refine_mode,
            user_request=user_request,
            original_result=None,
            warnings=warnings + ["missing qwen_result/pre_result/result"],
        )

    original_result = copy.deepcopy(qwen_result)
    task_nodes = normalize_task_nodes(original_result, warnings)
    original_edges = extract_edges_from_arguments(task_nodes, warnings)
    node_metadata = build_node_metadata(task_nodes, tool_catalog)
    candidate_edges = build_candidate_edges(node_metadata, transition_index)
    prompt = build_edge_critic_prompt(
        user_request=user_request,
        node_metadata=node_metadata,
        current_edges=original_edges,
        candidate_edges=candidate_edges,
        edge_refine_mode=edge_refine_mode,
    )

    raw_llm_output = ""
    parsed: Dict[str, Any] = {}
    try:
        raw_llm_output = client.chat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = extract_json_object(raw_llm_output)
    except Exception as exc:  # noqa: BLE001 - preserve row-level experiment output.
        warnings.append(f"llm_or_parse_error: {type(exc).__name__}: {exc}")

    requested_remove_edges = normalize_stage_edge_changes(
        parsed.get("remove_edges"),
        warnings=warnings,
        source_label="remove_edges",
        op_name="REMOVE_EDGE",
    )
    requested_add_edges = normalize_stage_edge_changes(
        parsed.get("add_edges"),
        warnings=warnings,
        source_label="add_edges",
        op_name="ADD_EDGE",
    )
    requested_add_edges = constrain_add_edges_by_mode(
        edge_refine_mode=edge_refine_mode,
        add_edges=requested_add_edges,
        remove_edges=requested_remove_edges,
        original_edges=original_edges,
        node_metadata=node_metadata,
        warnings=warnings,
    )
    refined_edges, stage1_removed_edges, stage2_added_edges = resolve_two_stage_final_edges(
        parsed=parsed,
        original_edges=original_edges,
        remove_edges=requested_remove_edges,
        add_edges=requested_add_edges,
        node_count=len(task_nodes),
        warnings=warnings,
    )
    edge_operations = stage1_removed_edges + stage2_added_edges
    edge_change_count = count_edge_changes(original_edges, refined_edges)
    refined_result = rebuild_result_edges(original_result, refined_edges, warnings)

    return {
        "ID": case_id,
        "edge_refine_mode": edge_refine_mode,
        "user_request": user_request,
        "original_result": original_result,
        "refined_result": refined_result,
        "edge_operations": edge_operations,
        "stage1_removed_edges": stage1_removed_edges,
        "stage2_added_edges": stage2_added_edges,
        "original_edges": original_edges,
        "final_edges": refined_edges,
        "refined_edges": refined_edges,
        "edge_change_count": edge_change_count,
        "raw_llm_output": raw_llm_output,
        "warnings": warnings,
        "debug": {
            "edge_refine_mode": edge_refine_mode,
            "node_metadata": node_metadata,
            "candidate_edges": candidate_edges,
            "llm_reason": parsed.get("reason") if isinstance(parsed, dict) else "",
            "gold_result_present": has_any_key(row, ["gold_result", "gold_pre", "gold"]),
        },
    }


def run_one_case_with_new_client(
    row: Dict[str, Any],
    case_id: str,
    llm_config: Any,
    llm_profile: str | None,
    tool_catalog: Dict[str, Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
    edge_refine_mode: str,
) -> Dict[str, Any]:
    client = OpenAICompatibleLLMClient(
        llm_config_path=llm_config,
        llm_profile=llm_profile,
    )
    return run_one_case(
        row=row,
        case_id=case_id,
        client=client,
        tool_catalog=tool_catalog,
        transition_index=transition_index,
        edge_refine_mode=edge_refine_mode,
    )


def build_error_result(
    case_id: str,
    edge_refine_mode: str,
    user_request: str,
    original_result: Any,
    warnings: List[str],
) -> Dict[str, Any]:
    return {
        "ID": case_id,
        "edge_refine_mode": edge_refine_mode,
        "user_request": user_request,
        "original_result": original_result,
        "refined_result": original_result,
        "edge_operations": [],
        "stage1_removed_edges": [],
        "stage2_added_edges": [],
        "original_edges": [],
        "final_edges": [],
        "refined_edges": [],
        "edge_change_count": 0,
        "raw_llm_output": "",
        "warnings": warnings,
        "debug": {},
    }


def extract_qwen_result(row: Dict[str, Any], warnings: List[str]) -> Dict[str, Any] | None:
    if "task_nodes" in row:
        return ensure_result_shape(parse_jsonish(row), warnings)

    for key in ["qwen_result", "pre_result", "result", "predicted_workflow", "predicted_result"]:
        if key not in row:
            continue
        payload = parse_jsonish(row.get(key))
        if isinstance(payload, dict):
            if "task_nodes" in payload:
                return ensure_result_shape(payload, warnings)
            nested = payload.get("result")
            if isinstance(nested, (dict, str)):
                nested_payload = parse_jsonish(nested)
                if isinstance(nested_payload, dict) and "task_nodes" in nested_payload:
                    return ensure_result_shape(nested_payload, warnings)

    nested_result = parse_jsonish(row.get("result"))
    if isinstance(nested_result, dict) and "task_nodes" in nested_result:
        return ensure_result_shape(nested_result, warnings)
    return None


def ensure_result_shape(payload: Dict[str, Any], warnings: List[str]) -> Dict[str, Any]:
    result = copy.deepcopy(payload)
    if not isinstance(result.get("task_steps"), list):
        result["task_steps"] = []
    if not isinstance(result.get("task_nodes"), list):
        warnings.append("qwen_result.task_nodes is not a list; set to empty list")
        result["task_nodes"] = []
    if not isinstance(result.get("task_links"), list):
        result["task_links"] = []
    return result


def normalize_task_nodes(result: Dict[str, Any], warnings: List[str]) -> List[Dict[str, Any]]:
    raw_nodes = result.get("task_nodes")
    if not isinstance(raw_nodes, list):
        warnings.append("task_nodes missing or invalid")
        return []

    normalized: List[Dict[str, Any]] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            warnings.append(f"task_nodes[{index}] is not an object; converted to empty node")
            node = {}
        if not isinstance(node.get("arguments"), list):
            if node.get("arguments") is None:
                node["arguments"] = []
            else:
                node["arguments"] = [node.get("arguments")]
        normalized.append(node)
    return normalized


def extract_edges_from_arguments(
    task_nodes: List[Dict[str, Any]],
    warnings: List[str],
) -> List[Dict[str, int]]:
    edges: set[Tuple[int, int]] = set()
    for target_index, node in enumerate(task_nodes):
        arguments = node.get("arguments", [])
        if not isinstance(arguments, list):
            continue
        for arg in arguments:
            source_index = parse_node_ref(arg)
            if source_index is None:
                continue
            if source_index >= target_index:
                warnings.append(
                    f"ignore invalid existing edge {source_index}->{target_index}: source must be < target"
                )
                continue
            if source_index < 0 or source_index >= len(task_nodes):
                warnings.append(
                    f"ignore invalid existing edge {source_index}->{target_index}: source index out of range"
                )
                continue
            edges.add((source_index, target_index))
    return edge_tuples_to_dicts(sorted(edges, key=lambda item: (item[1], item[0])))


def build_node_metadata(
    task_nodes: List[Dict[str, Any]],
    tool_catalog: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    metadata: List[Dict[str, Any]] = []
    for index, node in enumerate(task_nodes):
        task_name = str(node.get("task") or "").strip()
        tool = lookup_tool(tool_catalog, task_name)
        metadata.append(
            {
                "index": index,
                "task": task_name,
                "arguments": copy.deepcopy(node.get("arguments", [])),
                "input_type": list(tool.get("input_type", [])),
                "output_type": list(tool.get("output_type", [])),
                "desc": str(tool.get("desc", "")),
                "intent": str(tool.get("intent", "")),
            }
        )
    return metadata


def build_candidate_edges(
    node_metadata: List[Dict[str, Any]],
    transition_index: Dict[Tuple[str, str], Any],
) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for target_index, target_node in enumerate(node_metadata):
        for source_index in range(target_index):
            source_node = node_metadata[source_index]
            candidates.append(
                {
                    "source": source_index,
                    "target": target_index,
                    "source_tool": source_node.get("task", ""),
                    "target_tool": target_node.get("task", ""),
                    "source_output_type": source_node.get("output_type", []),
                    "target_input_type": target_node.get("input_type", []),
                    "type_compatible": type_compatible(
                        source_node.get("output_type", []),
                        target_node.get("input_type", []),
                    ),
                    "transition_probability": get_transition_probability(
                        transition_index,
                        source_node.get("task", ""),
                        target_node.get("task", ""),
                    ),
                }
            )
    return candidates


def build_stage2_prompt_rules(edge_refine_mode: str) -> str:
    if edge_refine_mode == "remove_only":
        return """Stage 2 is disabled in remove_only mode.

You must output:

"add_edges": []

Do not add any edge for any reason.
Do not recover missing edges.
Do not repair disconnected components.
Do not recover chains, forks, or merges.

The only allowed operation in this mode is REMOVE_EDGE in remove_edges."""

    if edge_refine_mode == "remove_plus_merge_add":
        return """Second, recover only narrow, high-confidence missing edges after Stage 1.

Stage 2 may only output ADD_EDGE entries in add_edges for the safe cases below.

Before adding an edge, check the target node's current incoming edges after Stage 1
and its literal arguments. Do not add an edge if the target already has enough
inputs from existing incoming edges plus user-provided literals.

Allowed ADD_EDGE cases:

1. Missing merge input.
   The target tool clearly requires multiple upstream artifacts, and the current incoming
   edges plus literal inputs after Stage 1 are fewer than the required inputs.

Typical examples:
- Video Synchronization requires video + audio.
- Audio Splicer requires audio + audio.
- Image Style Transfer requires content image + style image.
- Video Voiceover requires video + audio/text depending on tool input types.

2. Zero-indegree consumer.
   The target has no incoming edge and no literal argument after Stage 1, and the
   target task clearly consumes a previous workflow artifact rather than an
   original user literal.

Examples:
- URL Extractor consumes text produced by Text Paraphraser.
- Voice Changer consumes text instruction/transcription produced by Audio-to-Text.
- A checker/analyzer/summarizer consumes text produced by the previous text-processing step.

3. Wrong-predecessor replacement for merge tools.
   If a multi-input target currently has an obviously wrong predecessor, remove that wrong
   predecessor in remove_edges and add the correct predecessor in add_edges.
   Do not only add the correct predecessor without removing the wrong one.

Example:
- Video Synchronization should consume Audio Downloader output as audio, not Video Downloader
  output as the audio-side predecessor.

For ADD_EDGE:
Be stricter than removal.
Add an edge only when the target cannot be correctly executed without the source artifact.

Forbidden ADD_EDGE reasons in this mode:

- Do not add an edge when the target already has enough literal arguments and incoming edges.
- Do not add an edge for ordinary processing-chain recovery unless the target has zero indegree
  and no literal argument and clearly consumes the previous artifact.
- Do not add an edge for fork recovery unless it is a missing merge input or wrong-predecessor replacement.
- Do not add an edge only because source output type matches target input type.
- Do not add an edge only because transition_probability is high.
- Do not force edges between independent output tasks.
- Do not bypass an existing valid chain.
- Do not treat optional context as a necessary dependency."""

    return """Second, recover only high-confidence missing edges after Stage 1.

Stage 2 may only output ADD_EDGE entries in add_edges.

For ADD_EDGE:
Be stricter than removal.
Add an edge only when the target cannot be correctly executed without the source artifact.
Do not add an edge if the target already has enough inputs from existing incoming
edges plus user-provided literal arguments.

Allowed high-confidence ADD_EDGE cases:

1. Target indegree is 0, but target task clearly consumes a previous workflow output.
   This is allowed only when the target has no literal argument that can satisfy
   the required input.
   Example:
   previous node produces text,
   target task says summarize/analyze/rewrite/check the previous text.

2. Target tool requires multiple inputs, but current incoming edges are fewer than required.
   Examples:
   Video Synchronization requires video + audio.
   Image Style Transfer requires content image + style image.
   Audio Splicer requires audio + audio.

3. Current workflow has disconnected components, and user_request explicitly asks to combine,
   synchronize, merge, or use them together.

4. Fork recovery:
   A downstream node should consume an earlier shared artifact, not only the immediately previous branch output.

5. Processing-chain recovery:
   If the node sequence expresses a clear sequential transformation, preserve or recover chain edges
   between adjacent transformed artifacts.

Forbidden ADD_EDGE reasons:

- Do not add an edge when the target already has enough literal arguments and incoming edges.
- Do not add an edge only because source output type matches target input type.
- Do not add an edge only because transition_probability is high.
- Do not force edges between independent output tasks.
- Do not bypass an existing valid chain.
- Do not treat optional context as a necessary dependency."""


def build_edge_critic_prompt(
    user_request: str,
    node_metadata: List[Dict[str, Any]],
    current_edges: List[Dict[str, int]],
    candidate_edges: List[Dict[str, Any]],
    edge_refine_mode: str,
) -> str:
    payload = {
        "user_request": user_request,
        "workflow_nodes": node_metadata,
        "current_edges": current_edges,
        "candidate_edges": candidate_edges,
        "edge_refine_mode": edge_refine_mode,
    }
    stage2_rules = build_stage2_prompt_rules(edge_refine_mode)
    return f"""You are a Conservative Workflow Edge Refiner.

Your job is NOT to redesign the workflow.
Your job is NOT to find a better workflow.
Your job is ONLY to refine dependency edges conservatively.

Experiment Mode
===============

edge_refine_mode = {edge_refine_mode}

You must follow the mode-specific Stage 2 policy exactly.

You must not change:
- nodes
- tools
- node order
- task steps
- literal arguments

Input
=====

1. user_request
2. workflow_nodes
3. current_edges
4. candidate_edges
5. tool descriptions and input/output types
6. tool transition information

Workflow Semantics
==================

Treat the workflow as a processing chain unless the user request clearly implies branching or merging.

A downstream node often intentionally consumes the latest transformed artifact.

Example:

Text Simplifier
->
Text Paraphraser
->
Grammar Checker
->
Sentiment Analysis

Do not bypass valid intermediate transformations.

An argument like "<node-i>" is a node-reference dependency, not a user-provided literal.
Do not remove an edge merely because the target arguments already contain "<node-i>";
that reference is evidence for the dependency.

==================================================
Stage 1: Current Edge Validation
==================================================

First, validate existing current_edges.

Stage 1 may only output REMOVE_EDGE entries in remove_edges.

Be conservative. Remove only obvious errors.

You may remove an existing edge only when at least one condition is true:

1. Type-incompatible edge
   The source output type and target input type are completely incompatible.

2. Impossible dependency
   The source cannot produce the artifact required by the target.

3. User-request violation
   The edge clearly contradicts the user-requested dataflow.

Do NOT remove an edge because:
- another predecessor looks more relevant
- another predecessor is closer to the original input
- another predecessor has higher transition_probability
- the workflow could be shorter
- the workflow could be more efficient

Default decision for existing edges: KEEP.

==================================================
Stage 2: Missing Edge Recovery
==================================================

{stage2_rules}

Transition probabilities are supporting evidence only. They are never sufficient by themselves.

Output JSON Only
================

{{
  "remove_edges": [
    {{
      "source": 0,
      "target": 2,
      "reason": "..."
    }}
  ],
  "add_edges": [
    {{
      "source": 1,
      "target": 3,
      "reason": "..."
    }}
  ],
  "final_edges": [
    {{
      "source": 0,
      "target": 1
    }}
  ],
  "reason": "..."
}}

Hard Constraints
================

Do not output ADD_NODE.
Do not output DELETE_NODE.
Do not output REPLACE_TOOL.
Do not do task splitting.
Do not do intent repair.
Do not do coverage repair.

If no obvious edge refinement is needed:

{{
  "remove_edges": [],
  "add_edges": [],
  "final_edges": current_edges,
  "reason": "No obvious dependency error or high-confidence missing edge."
}}

Input
=====

{json.dumps(payload, ensure_ascii=False, indent=2)}
"""


def normalize_stage_edge_changes(
    raw_edges: Any,
    warnings: List[str],
    source_label: str,
    op_name: str,
) -> List[Dict[str, Any]]:
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
                "reason": str(item.get("reason") or ""),
            }
        )
    return normalized


def constrain_add_edges_by_mode(
    edge_refine_mode: str,
    add_edges: List[Dict[str, Any]],
    remove_edges: List[Dict[str, Any]],
    original_edges: List[Dict[str, int]],
    node_metadata: List[Dict[str, Any]],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    if not add_edges:
        return []

    if edge_refine_mode == "remove_only":
        warnings.append(f"remove_only mode ignored {len(add_edges)} add_edges")
        return []

    if edge_refine_mode not in {"remove_plus_merge_add", "two_stage"}:
        warnings.append(f"unknown edge_refine_mode={edge_refine_mode}; add_edges ignored")
        return []

    edge_set_after_remove = edge_key_set(original_edges)
    node_count = len(node_metadata)
    for edge in remove_edges:
        source = edge.get("source")
        target = edge.get("target")
        if (
            isinstance(source, int)
            and isinstance(target, int)
            and is_valid_edge_tuple(source, target, node_count)
        ):
            edge_set_after_remove.discard((source, target))

    filtered: List[Dict[str, Any]] = []
    for index, edge in enumerate(add_edges):
        source = edge["source"]
        target = edge["target"]
        if not is_valid_edge_tuple(source, target, node_count):
            warnings.append(
                f"{edge_refine_mode} ignored add_edges[{index}] {source}->{target}: invalid edge"
            )
            continue
        if (source, target) in edge_set_after_remove:
            warnings.append(
                f"{edge_refine_mode} ignored add_edges[{index}] {source}->{target}: already exists"
            )
            continue

        target_node = node_metadata[target]
        source_node = node_metadata[source]
        required_count = required_input_count(target_node)
        current_indegree = sum(1 for _, edge_target in edge_set_after_remove if edge_target == target)
        removed_same_target = any(edge.get("target") == target for edge in remove_edges)
        compatibility = type_compatible(
            source_node.get("output_type", []),
            target_node.get("input_type", []),
        )

        if compatibility is not True:
            warnings.append(
                f"{edge_refine_mode} ignored add_edges[{index}] {source}->{target}: type_compatible={compatibility}"
            )
            continue
        safe_add = classify_strict_safe_add_edge(
            source=source,
            target=target,
            source_node=source_node,
            target_node=target_node,
            required_count=required_count,
            current_indegree=current_indegree,
            literal_count=literal_input_match_count(target_node),
            removed_same_target=removed_same_target,
            reason=edge.get("reason", ""),
        )
        if safe_add is None:
            warnings.append(
                f"{edge_refine_mode} ignored add_edges[{index}] {source}->{target}: not a strict safe-add case"
            )
            continue

        edge["gate_reason"] = safe_add
        filtered.append(edge)
        edge_set_after_remove.add((source, target))

    return filtered


def classify_strict_safe_add_edge(
    source: int,
    target: int,
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
    required_count: int,
    current_indegree: int,
    literal_count: int,
    removed_same_target: bool,
    reason: str,
) -> str | None:
    """Return the accepted gate reason for a high-confidence ADD_EDGE.

    The strict gate prevents the old broad rule where any zero-indegree target
    could receive an edge. A target must still have an unsatisfied input slot
    after considering both incoming edges and literal user inputs.
    """
    if required_count <= 0:
        return None

    filled_count = current_indegree + literal_count
    if filled_count >= required_count:
        return None

    missing_count = required_count - filled_count
    if required_count >= 2:
        if removed_same_target:
            return "replace_wrong_multi_input_predecessor"
        if is_merge_like_target(target_node):
            return "missing_merge_input"
        if missing_count > 0 and current_indegree > 0:
            return "missing_multi_input_slot"
        return None

    if current_indegree != 0 or literal_count != 0:
        return None

    if source + 1 == target and is_processing_chain_add(source_node, target_node):
        return "adjacent_zero_input_chain"

    if has_explicit_fork_or_shared_artifact_reason(str(reason or "")):
        return "explicit_fork_or_shared_artifact"

    return None


def literal_input_match_count(node_metadata: Dict[str, Any]) -> int:
    arguments = node_metadata.get("arguments", [])
    if not isinstance(arguments, list):
        arguments = [] if arguments is None else [arguments]

    available_inputs = [
        normalize_type_name(item)
        for item in coerce_str_list(node_metadata.get("input_type", []))
    ]
    matched_count = 0
    for argument in arguments:
        if parse_node_ref(argument) is not None:
            continue
        literal_type = infer_literal_argument_type(argument)
        if literal_type in available_inputs:
            matched_count += 1
            available_inputs.remove(literal_type)
    return matched_count


def infer_literal_argument_type(argument: Any) -> str:
    text = str(argument or "").strip().lower()
    if not text:
        return ""
    if re.match(r"^[a-z][a-z0-9+.-]*://", text):
        return "url"
    if any(f".{extension}" in text for extension in ["jpg", "jpeg", "png", "gif", "bmp", "tiff", "svg", "ico"]):
        return "image"
    if any(f".{extension}" in text for extension in ["mp3", "wav", "wma", "ogg", "aac", "flac", "aiff", "au"]):
        return "audio"
    if any(f".{extension}" in text for extension in ["mp4", "avi", "mov", "flv", "wmv", "mkv", "webm", "m4v", "mpg", "mpeg"]):
        return "video"
    return "text"


def is_merge_like_target(node_metadata: Dict[str, Any]) -> bool:
    text = " ".join(
        [
            str(node_metadata.get("task", "")),
            str(node_metadata.get("desc", "")),
            str(node_metadata.get("intent", "")),
        ]
    ).lower()
    keywords = [
        "synchronization",
        "synchronize",
        "splicer",
        "splice",
        "stitcher",
        "stitch",
        "style transfer",
        "voiceover",
        "voice over",
        "combine",
        "merge",
        "collage",
        "slideshow",
        "image-to-video",
    ]
    return any(keyword in text for keyword in keywords)


def is_processing_chain_add(
    source_node: Dict[str, Any],
    target_node: Dict[str, Any],
) -> bool:
    source_types = normalize_type_set(source_node.get("output_type", []))
    target_types = normalize_type_set(target_node.get("input_type", []))
    if not source_types or not target_types or not (source_types & target_types):
        return False

    target_task = str(target_node.get("task", "")).lower()
    target_desc = str(target_node.get("desc", "")).lower()
    target_text = f"{target_task} {target_desc}"
    processing_keywords = [
        "summar",
        "simplif",
        "paraphras",
        "rewrite",
        "grammar",
        "sentiment",
        "keyword",
        "extract",
        "translat",
        "expand",
        "effect",
        "noise reduction",
        "stabiliz",
        "speed",
        "coloriz",
        "download",
        "search",
        "generate",
        "convert",
        "transcribe",
    ]
    return any(keyword in target_text for keyword in processing_keywords)


def has_explicit_fork_or_shared_artifact_reason(reason: str) -> bool:
    text = reason.lower()
    keywords = [
        "fork",
        "shared artifact",
        "shared input",
        "also consume",
        "also consumes",
        "same artifact",
        "same source",
        "branch",
    ]
    return any(keyword in text for keyword in keywords)


def required_input_count(node_metadata: Dict[str, Any]) -> int:
    return len(coerce_str_list(node_metadata.get("input_type", [])))


def resolve_two_stage_final_edges(
    parsed: Dict[str, Any],
    original_edges: List[Dict[str, int]],
    remove_edges: List[Dict[str, Any]],
    add_edges: List[Dict[str, Any]],
    node_count: int,
    warnings: List[str],
) -> Tuple[List[Dict[str, int]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    edge_set = {(edge["source"], edge["target"]) for edge in original_edges}
    applied_removed: List[Dict[str, Any]] = []
    applied_added: List[Dict[str, Any]] = []

    for index, edge in enumerate(remove_edges):
        source = edge["source"]
        target = edge["target"]
        if not is_valid_edge_tuple(source, target, node_count):
            warnings.append(f"remove_edges[{index}] {source}->{target} invalid; ignored")
            continue
        if (source, target) not in edge_set:
            warnings.append(f"remove_edges[{index}] {source}->{target} not in current edges; ignored")
            continue
        edge_set.discard((source, target))
        applied_removed.append(edge)

    for index, edge in enumerate(add_edges):
        source = edge["source"]
        target = edge["target"]
        if not is_valid_edge_tuple(source, target, node_count):
            warnings.append(f"add_edges[{index}] {source}->{target} invalid; ignored")
            continue
        if (source, target) in edge_set:
            warnings.append(f"add_edges[{index}] {source}->{target} already exists; ignored")
            continue
        edge_set.add((source, target))
        applied_added.append(edge)

    final_edges = edge_tuples_to_dicts(sorted(edge_set, key=lambda item: (item[1], item[0])))
    validated_edges = validate_edges(final_edges, node_count, warnings)
    if len(validated_edges) != len(final_edges):
        warnings.append("validated final_edges differ from computed final_edges; fall back to original_edges")
        return original_edges, [], []

    raw_final_edges = parsed.get("final_edges") if isinstance(parsed, dict) else None
    if raw_final_edges is not None:
        llm_final_edges = validate_edges(
            normalize_edge_list(raw_final_edges, warnings, source_label="final_edges"),
            node_count,
            warnings,
        )
        if edge_key_set(llm_final_edges) != edge_key_set(validated_edges):
            warnings.append("llm final_edges differs from staged remove/add result; staged result used")

    return validated_edges, applied_removed, applied_added


def is_valid_edge_tuple(source: int, target: int, node_count: int) -> bool:
    return (
        0 <= source < node_count
        and 0 <= target < node_count
        and source < target
    )


def edge_key_set(edges: List[Dict[str, int]]) -> set[Tuple[int, int]]:
    return {(edge["source"], edge["target"]) for edge in edges}


def count_edge_changes(
    original_edges: List[Dict[str, int]],
    final_edges: List[Dict[str, int]],
) -> int:
    return len(edge_key_set(original_edges) ^ edge_key_set(final_edges))


def normalize_edge_list(
    raw_edges: Any,
    warnings: List[str],
    source_label: str,
) -> List[Dict[str, int]]:
    if not isinstance(raw_edges, list):
        warnings.append(f"{source_label} is not a list; ignored")
        return []

    edges: List[Dict[str, int]] = []
    for index, item in enumerate(raw_edges):
        source: int | None = None
        target: int | None = None
        if isinstance(item, dict):
            source = coerce_int(item.get("source"))
            target = coerce_int(item.get("target"))
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            source = coerce_int(item[0])
            target = coerce_int(item[1])
        if source is None or target is None:
            warnings.append(f"{source_label}[{index}] missing int source/target; ignored")
            continue
        edges.append({"source": source, "target": target})
    return edges


def validate_edges(
    edges: List[Dict[str, int]],
    node_count: int,
    warnings: List[str],
) -> List[Dict[str, int]]:
    valid: set[Tuple[int, int]] = set()
    for index, edge in enumerate(edges):
        source = coerce_int(edge.get("source"))
        target = coerce_int(edge.get("target"))
        if source is None or target is None:
            warnings.append(f"drop edge[{index}]: source/target is not int")
            continue
        if source == target:
            warnings.append(f"drop edge[{index}] {source}->{target}: self-loop")
            continue
        if source < 0 or target < 0 or source >= node_count or target >= node_count:
            warnings.append(f"drop edge[{index}] {source}->{target}: node index out of range")
            continue
        if source >= target:
            warnings.append(f"drop edge[{index}] {source}->{target}: source must be < target")
            continue
        valid.add((source, target))
    return edge_tuples_to_dicts(sorted(valid, key=lambda item: (item[1], item[0])))


def rebuild_result_edges(
    original_result: Dict[str, Any],
    final_edges: List[Dict[str, int]],
    warnings: List[str],
) -> Dict[str, Any]:
    refined = copy.deepcopy(original_result)
    task_nodes = refined.get("task_nodes")
    if not isinstance(task_nodes, list):
        warnings.append("cannot rebuild arguments: task_nodes is not a list")
        refined["task_links"] = []
        return refined

    incoming_by_target: Dict[int, List[int]] = {}
    for edge in final_edges:
        incoming_by_target.setdefault(edge["target"], []).append(edge["source"])

    for index, node in enumerate(task_nodes):
        if not isinstance(node, dict):
            continue
        old_arguments = node.get("arguments", [])
        if not isinstance(old_arguments, list):
            old_arguments = [] if old_arguments is None else [old_arguments]
        literal_arguments = [
            copy.deepcopy(arg)
            for arg in old_arguments
            if parse_node_ref(arg) is None
        ]
        node_refs = [
            f"<node-{source}>"
            for source in sorted(set(incoming_by_target.get(index, [])))
        ]
        node["arguments"] = node_refs + literal_arguments

    refined["task_links"] = build_task_links(task_nodes, final_edges)
    return refined


def build_task_links(
    task_nodes: List[Dict[str, Any]],
    edges: List[Dict[str, int]],
) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    for edge in edges:
        source_node = task_nodes[edge["source"]]
        target_node = task_nodes[edge["target"]]
        links.append(
            {
                "source": str(source_node.get("task") or ""),
                "target": str(target_node.get("task") or ""),
            }
        )
    return links


def parse_node_ref(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = NODE_REF_RE.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1))


def edge_tuples_to_dicts(edges: Iterable[Tuple[int, int]]) -> List[Dict[str, int]]:
    return [{"source": source, "target": target} for source, target in edges]


def type_compatible(source_output: Any, target_input: Any) -> bool | str:
    target_types = normalize_type_set(target_input)
    if not target_types:
        return False
    source_types = normalize_type_set(source_output)
    if not source_types:
        return "unknown"
    if "any" in target_types or "*" in target_types:
        return True
    return bool(source_types & target_types)


def normalize_type_set(value: Any) -> set[str]:
    return {
        normalize_type_name(item)
        for item in coerce_list(value)
        if normalize_type_name(item)
    }


def normalize_type_name(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-")


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
        tool_id = str(
            item.get("id")
            or item.get("tool_id")
            or item.get("name")
            or item.get("task")
            or ""
        ).strip()
        if not tool_id:
            continue
        record = {
            "id": tool_id,
            "desc": str(item.get("desc") or item.get("description") or ""),
            "intent": str(item.get("intent") or ""),
            "input_type": coerce_str_list(
                item.get("input-type")
                or item.get("input_type")
                or item.get("input_types")
            ),
            "output_type": coerce_str_list(
                item.get("output-type")
                or item.get("output_type")
                or item.get("output_types")
            ),
        }
        catalog[normalize_tool_key(tool_id)] = record
    return catalog


def lookup_tool(tool_catalog: Dict[str, Dict[str, Any]], task_name: str) -> Dict[str, Any]:
    key = normalize_tool_key(task_name)
    if key in tool_catalog:
        return tool_catalog[key]

    stripped_call = re.sub(r"\s*\(.*\)\s*$", "", str(task_name)).strip()
    key = normalize_tool_key(stripped_call)
    if key in tool_catalog:
        return tool_catalog[key]

    return {
        "id": task_name,
        "desc": "",
        "intent": "",
        "input_type": [],
        "output_type": [],
    }


def normalize_tool_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower().replace("_", " "))


def load_transition_index(raw_path: Any) -> Dict[Tuple[str, str], Any]:
    if not raw_path:
        return {}
    path = resolve_path(raw_path)
    if not path.exists():
        raise FileNotFoundError(f"transition graph not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    index: Dict[Tuple[str, str], Any] = {}

    for edge in coerce_list(payload.get("edges") if isinstance(payload, dict) else None):
        if isinstance(edge, dict):
            add_transition_edge(index, edge)
    for edge in coerce_list(payload.get("links") if isinstance(payload, dict) else None):
        if isinstance(edge, dict):
            add_transition_edge(index, edge)

    adjacency = payload.get("adjacency") if isinstance(payload, dict) else None
    if isinstance(adjacency, dict):
        for source, raw_targets in adjacency.items():
            if isinstance(raw_targets, dict):
                for target, value in raw_targets.items():
                    probability = extract_probability(value)
                    index[(normalize_tool_key(source), normalize_tool_key(target))] = probability
            elif isinstance(raw_targets, list):
                for item in raw_targets:
                    if not isinstance(item, dict):
                        continue
                    target = (
                        item.get("target_tool_id")
                        or item.get("target")
                        or item.get("target_tool")
                    )
                    if not target:
                        continue
                    probability = extract_probability(item)
                    index[(normalize_tool_key(source), normalize_tool_key(target))] = probability
    return index


def add_transition_edge(index: Dict[Tuple[str, str], Any], edge: Dict[str, Any]) -> None:
    source = edge.get("source_tool_id") or edge.get("source") or edge.get("from")
    target = edge.get("target_tool_id") or edge.get("target") or edge.get("to")
    if not source or not target:
        return
    index[(normalize_tool_key(source), normalize_tool_key(target))] = extract_probability(edge)


def extract_probability(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    if not isinstance(value, dict):
        return None
    for key in ["transition_probability", "probability", "prob", "weight", "score"]:
        if key in value:
            number = coerce_float(value.get(key))
            if number is not None:
                return number
    return None


def get_transition_probability(
    transition_index: Dict[Tuple[str, str], Any],
    source_tool: Any,
    target_tool: Any,
) -> Any:
    if not transition_index:
        return None
    return transition_index.get((normalize_tool_key(source_tool), normalize_tool_key(target_tool)))


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
    value = get_first_value(row, ["ID", "id", "case_id"])
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


def has_any_key(row: Dict[str, Any], keys: List[str]) -> bool:
    lowered = {str(key).lower() for key in row.keys()}
    return any(key.lower() in lowered for key in keys)


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
            else:
                print(f"[WARN] skip non-object JSONL line {line_number}")
    return rows


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_existing_results(path: Path) -> Dict[str, Dict[str, Any]]:
    if not path.exists():
        return {}
    return {
        str(row.get("ID") or "").strip(): row
        for row in read_jsonl_records(path)
        if str(row.get("ID") or "").strip()
    }


def read_xlsx_records(path: Path) -> List[Dict[str, Any]]:
    rows = read_xlsx_rows(path)
    if not rows:
        return []
    header = rows[0]
    records: List[Dict[str, Any]] = []
    for row in rows[1:]:
        record = {
            header[index]: row[index] if index < len(row) else ""
            for index in range(len(header))
        }
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
        return shared_strings[int(value.text)]
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
        xlsx_rows.append(
            [
                row.get("ID", ""),
                row.get("edge_refine_mode", ""),
                row.get("user_request", ""),
                json_compact(row.get("original_edges", [])),
                json_compact(row.get("stage1_removed_edges", [])),
                json_compact(row.get("stage2_added_edges", [])),
                json_compact(row.get("final_edges", row.get("refined_edges", []))),
                row.get("edge_change_count", ""),
                json_compact(row.get("warnings", [])),
                json_compact(row.get("original_result", {})),
                json_compact(row.get("refined_result", {})),
                row.get("raw_llm_output", ""),
            ]
        )
    write_xlsx_rows(path, xlsx_rows)


def json_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ": "))


def write_xlsx_rows(path: Path, rows: List[List[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dim = f"A1:{column_name(len(XLSX_HEADERS) - 1)}{len(rows)}"
    widths = [16, 22, 70, 32, 46, 46, 32, 18, 52, 92, 92, 92]
    cols = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, 1)
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
  <sheets><sheet name="edge_refine" sheetId="1" r:id="rId1"/></sheets>
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


if __name__ == "__main__":
    raise SystemExit(main())
