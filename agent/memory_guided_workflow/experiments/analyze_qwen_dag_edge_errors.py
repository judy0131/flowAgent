# -*- coding: utf-8 -*-
"""Sample DAG badcases and classify workflow edge errors with an LLM.

This experiment is analysis-only. It does not repair workflows and does not
modify prediction, evaluation, or planner runtime code.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List


REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.memory_guided_workflow.llm_client import OpenAICompatibleLLMClient
from agent.memory_guided_workflow.utils import extract_json_object


DEFAULT_BADCASE_CSV = (
    "taskbench/data_multimedia/metrics_use_demos_2_reformat_by_self/"
    "three_tables_qwen3-14b_use_demos_2_reformat_by_self/"
    "03_badcase_report_20260527.csv"
)
DEFAULT_PREDICTIONS = (
    "taskbench/data_multimedia/predictions_use_demos_2_reformat_by_self/"
    "qwen3-14b_20260527.json"
)
DEFAULT_GOLD = "taskbench/data_multimedia/data.json"
DEFAULT_TOOL_DESC = "taskbench/data_multimedia/tool_desc.json"
DEFAULT_OUTPUT_DIR = (
    "agent/memory_guided_workflow/metrics/miwp_tree_tables/"
    "20260606-qwen3-14b-edge-error-analysis"
)

ALLOWED_STAGES = {
    "predecessor_selection",
    "dependency_reasoning",
    "task_understanding",
    "tool_selection",
    "workflow_repair",
    "other",
}
NODE_REF_RE = re.compile(r"^<node-(\d+)>$")


def main() -> int:
    args = parse_args()
    out_dir = resolve_path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    badcase_rows = read_csv(resolve_path(args.badcase_csv))
    prediction_by_id = {
        str(row.get("id", "")).strip(): row
        for row in read_jsonl(resolve_path(args.predictions))
    }
    gold_by_id = {
        str(row.get("id", "")).strip(): row
        for row in read_jsonl(resolve_path(args.gold))
    }
    tool_io = load_tool_io(resolve_path(args.tool_desc))

    raw_candidates = [
        row
        for row in badcase_rows
        if str(row.get("Type", "")).strip().lower() == "dag"
    ]
    candidate_payloads = [
        build_case_payload(row, prediction_by_id, gold_by_id, tool_io)
        for row in raw_candidates
    ]
    candidate_payloads = [
        payload
        for payload in candidate_payloads
        if not edge_sets_equal(
            payload["gold_workflow"].get("task_links", []),
            payload["predicted_workflow"].get("task_links", []),
        )
    ]
    if len(candidate_payloads) < args.sample_size:
        raise RuntimeError(
            f"not enough materialized DAG edge badcases: {len(candidate_payloads)} < {args.sample_size}"
        )

    rng = random.Random(args.seed)
    sample_payloads = rng.sample(candidate_payloads, args.sample_size)

    sample_path = out_dir / "sampled_dag_edge_badcases_20260527_seed20260606.jsonl"
    write_jsonl(sample_path, sample_payloads)
    if args.sample_only:
        print(f"[DONE] sample={sample_path}")
        print("[SUMMARY] " + json.dumps({"sample_size": len(sample_payloads)}, ensure_ascii=False))
        return 0

    result_path = out_dir / "dag_edge_error_analysis_qwen3-14b_20260527_sample50.jsonl"
    existing = load_existing_results(result_path) if args.resume else {}
    client = OpenAICompatibleLLMClient(
        llm_config_path=args.llm_config,
        llm_profile=args.llm_profile,
    )

    for index, payload in enumerate(sample_payloads, start=1):
        case_id = payload["id"]
        if args.resume and case_id in existing:
            print(f"[{index}/{len(sample_payloads)}] skip id={case_id} (resume)")
            continue

        print(f"[{index}/{len(sample_payloads)}] analyzing id={case_id}")
        prompt = build_prompt(payload)
        raw_text = client.chat(
            messages=[
                {"role": "system", "content": "Return JSON only."},
                {"role": "user", "content": prompt},
            ]
        )
        parsed = extract_json_object(raw_text)
        normalized = normalize_analysis(parsed)
        result = {
            "id": case_id,
            "type": payload["type"],
            "badcase_error_summary": payload["badcase_error_summary"],
            "analysis": normalized,
            "raw_output": raw_text,
        }
        append_jsonl(result_path, result)
        existing[case_id] = result

    ordered_results = [
        existing[payload["id"]]
        for payload in sample_payloads
        if payload["id"] in existing
    ]
    write_jsonl(result_path, ordered_results)

    csv_path = out_dir / "dag_edge_error_analysis_qwen3-14b_20260527_sample50.csv"
    summary_path = out_dir / "dag_edge_error_analysis_summary_qwen3-14b_20260527_sample50.json"
    write_result_csv(csv_path, ordered_results)
    summary = build_summary(ordered_results)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[DONE] sample={sample_path}")
    print(f"[DONE] result_jsonl={result_path}")
    print(f"[DONE] result_csv={csv_path}")
    print(f"[DONE] summary={summary_path}")
    print("[SUMMARY] " + json.dumps(summary, ensure_ascii=False))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Classify DAG workflow edge errors.")
    parser.add_argument("--badcase-csv", default=DEFAULT_BADCASE_CSV)
    parser.add_argument("--predictions", default=DEFAULT_PREDICTIONS)
    parser.add_argument("--gold", default=DEFAULT_GOLD)
    parser.add_argument("--tool-desc", default=DEFAULT_TOOL_DESC)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sample-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260606)
    parser.add_argument("--llm-config", default="configs/qwen.json")
    parser.add_argument("--llm-profile", default=None)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sample-only", action="store_true")
    return parser.parse_args()


def resolve_path(raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    return (REPO_ROOT / path).resolve()


def read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[WARN] skip invalid JSON line {line_number} in {path.name}: {exc}")
                continue
            if isinstance(payload, dict):
                rows.append(payload)
    return rows


def write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def append_jsonl(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_existing_results(path: Path) -> Dict[str, Dict[str, Any]]:
    return {
        str(row.get("id", "")).strip(): row
        for row in read_jsonl(path)
        if str(row.get("id", "")).strip()
    } if path.exists() else {}


def parse_maybe_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value if value is not None else default


def load_tool_io(path: Path) -> Dict[str, Dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    nodes = payload.get("nodes", payload) if isinstance(payload, dict) else payload
    result: Dict[str, Dict[str, Any]] = {}
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        tool_id = str(node.get("id", "")).strip()
        if not tool_id:
            continue
        result[normalize_name(tool_id)] = {
            "id": tool_id,
            "intent": str(node.get("intent", "")).strip(),
            "input_type": node.get("input-type", []),
            "output_type": node.get("output-type", []),
            "desc": str(node.get("desc", "")).strip(),
        }
    return result


def normalize_name(value: Any) -> str:
    return str(value).replace("_", " ").strip().lower()


def display_name(value: Any) -> str:
    return str(value).replace("_", " ").strip()


def materialize_links_from_arguments(task_nodes: Any) -> List[Dict[str, str]]:
    if not isinstance(task_nodes, list):
        return []
    node_names = [
        display_name(node.get("task", ""))
        for node in task_nodes
        if isinstance(node, dict)
    ]
    links: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for target_index, node in enumerate(task_nodes):
        if not isinstance(node, dict):
            continue
        target = node_names[target_index] if target_index < len(node_names) else ""
        arguments = node.get("arguments", [])
        if not isinstance(arguments, list):
            arguments = []
        for argument in arguments:
            if isinstance(argument, dict):
                values = list(argument.values())
                argument = values[0] if values else ""
            if not isinstance(argument, str):
                continue
            match = NODE_REF_RE.fullmatch(argument.strip())
            if match is None:
                continue
            source_index = int(match.group(1))
            if not (0 <= source_index < target_index < len(node_names)):
                continue
            source = node_names[source_index]
            if not source or not target:
                continue
            key = (source, target)
            if key in seen:
                continue
            seen.add(key)
            links.append({"source": source, "target": target})
    return links


def normalize_links(task_links: Any) -> List[Dict[str, str]]:
    links: List[Dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in task_links if isinstance(task_links, list) else []:
        if not isinstance(link, dict):
            continue
        source = display_name(link.get("source", ""))
        target = display_name(link.get("target", ""))
        if not source or not target:
            continue
        key = (source, target)
        if key in seen:
            continue
        seen.add(key)
        links.append({"source": source, "target": target})
    return links


def edge_sets_equal(left_links: Any, right_links: Any) -> bool:
    left = {
        (normalize_name(link.get("source", "")), normalize_name(link.get("target", "")))
        for link in left_links if isinstance(link, dict)
    }
    right = {
        (normalize_name(link.get("source", "")), normalize_name(link.get("target", "")))
        for link in right_links if isinstance(link, dict)
    }
    return left == right


def build_case_payload(
    badcase_row: Dict[str, str],
    prediction_by_id: Dict[str, Dict[str, Any]],
    gold_by_id: Dict[str, Dict[str, Any]],
    tool_io: Dict[str, Dict[str, Any]],
) -> Dict[str, Any]:
    case_id = str(badcase_row.get("ID", "")).strip()
    pred = prediction_by_id.get(case_id)
    gold = gold_by_id.get(case_id)
    if pred is None:
        raise KeyError(f"prediction not found for id={case_id}")
    if gold is None:
        raise KeyError(f"gold not found for id={case_id}")

    gold_nodes = parse_maybe_json(gold.get("tool_nodes", []), [])
    raw_gold_links = normalize_links(parse_maybe_json(gold.get("tool_links", []), []))
    gold_materialized_links = materialize_links_from_arguments(gold_nodes)
    gold_links = gold_materialized_links or raw_gold_links
    pred_result = pred.get("result", {}) if isinstance(pred.get("result"), dict) else {}
    pred_nodes = pred_result.get("task_nodes", [])
    raw_pred_links = normalize_links(pred_result.get("task_links", []))
    pred_materialized_links = materialize_links_from_arguments(pred_nodes)
    pred_links = pred_materialized_links or raw_pred_links

    node_names = set()
    for node in list(gold_nodes if isinstance(gold_nodes, list) else []) + list(pred_nodes if isinstance(pred_nodes, list) else []):
        if isinstance(node, dict):
            name = normalize_name(node.get("task", ""))
            if name:
                node_names.add(name)

    return {
        "id": case_id,
        "type": str(badcase_row.get("Type", "")).strip(),
        "badcase_error_summary": str(badcase_row.get("Error Summary", "")).strip(),
        "user_request": str(pred.get("user_request") or gold.get("instruction") or "").strip(),
        "gold_workflow": {
            "task_nodes": gold_nodes,
            "task_links": gold_links,
            "raw_task_links": raw_gold_links,
            "materialized_task_links": gold_materialized_links,
        },
        "predicted_workflow": {
            "task_nodes": pred_nodes,
            "task_links": pred_links,
            "raw_task_links": raw_pred_links,
            "materialized_task_links": pred_materialized_links,
        },
        "tool_type_reference": [
            tool_io[name]
            for name in sorted(node_names)
            if name in tool_io
        ],
    }


def build_prompt(payload: Dict[str, Any]) -> str:
    compact = json.dumps(payload, ensure_ascii=False, indent=2)
    return f"""
Goal:

Analyze why a predicted workflow edge structure differs from the gold workflow.

You are NOT repairing the workflow.

You are ONLY classifying edge prediction errors.

==================================================
Input
=====

1. user_request

2. gold_workflow

3. predicted_workflow

Each workflow contains:

{{
"task_nodes": [...],
"task_links": [...]
}}

The input also includes tool_type_reference only to help judge type
compatibility. Do not use it to propose repairs.

==================================================
Analysis Procedure
==================

Compare the predicted workflow against the gold workflow.

Focus only on workflow dependencies.

Ignore argument differences.

Ignore tool parameter differences.

Ignore node naming differences if the semantic meaning is equivalent.

==================================================
Edge Error Categories
=====================

Classify every error into one or more categories.

Category A: Missing Edge

A dependency exists in the gold workflow but is absent in the prediction.

---

Category B: Extra Edge

A dependency exists in the prediction but does not exist in the gold workflow.

---

Category C: Wrong Predecessor

The target node is correct, but the selected predecessor node is wrong.

Typical examples:

gold:
A -> C
B -> C

predicted:
A -> C

or

predicted:
B -> C

when both are required.

---

Category D: Type-Incompatible Edge

The predicted edge connects nodes whose outputs and inputs are semantically incompatible.

Example:

Text output
→
Audio Effects

---

Category E: Fork-vs-Chain Error

The gold workflow contains a branch structure, but the prediction converts it into a chain.

Example:

gold:
A -> B
A -> C

predicted:
A -> B -> C

---

Category F: Merge Error

The gold workflow requires multiple predecessors.

Example:

A -> D
B -> D

but prediction only connects one predecessor.

---

Category G: Other

Any edge error not covered above.

==================================================
Output JSON Only
================

{{
"has_edge_error": true,

"error_categories": [
{{
"category": "Missing Edge",
"description": "..."
}}
],

"root_cause": "...",

"most_likely_failure_stage": [
"predecessor_selection",
"dependency_reasoning",
"task_understanding",
"tool_selection",
"workflow_repair",
"other"
],

"reason": "..."
}}

==================================================
Important Constraints
=====================

Do NOT propose repairs.

Do NOT generate new workflows.

Do NOT generate new edges.

Only classify the failure.

Focus on identifying where the mistake came from.

==================================================
Case Input
==========

{compact}
""".strip()


def normalize_analysis(payload: Dict[str, Any]) -> Dict[str, Any]:
    categories = []
    for item in payload.get("error_categories", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        category = str(item.get("category", "")).strip()
        description = str(item.get("description", "")).strip()
        if not category:
            continue
        categories.append({"category": category, "description": description})

    stages = []
    for stage in payload.get("most_likely_failure_stage", []) if isinstance(payload, dict) else []:
        stage_text = str(stage).strip()
        if stage_text in ALLOWED_STAGES and stage_text not in stages:
            stages.append(stage_text)
    if not stages:
        stages = ["other"]

    return {
        "has_edge_error": bool(payload.get("has_edge_error", True)),
        "error_categories": categories,
        "root_cause": str(payload.get("root_cause", "")).strip(),
        "most_likely_failure_stage": stages,
        "reason": str(payload.get("reason", "")).strip(),
    }


def write_result_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    output_rows = []
    for row in rows:
        analysis = row.get("analysis", {})
        categories = analysis.get("error_categories", []) if isinstance(analysis, dict) else []
        output_rows.append(
            {
                "ID": row.get("id", ""),
                "Type": row.get("type", ""),
                "Badcase Error Summary": row.get("badcase_error_summary", ""),
                "Has Edge Error": analysis.get("has_edge_error", ""),
                "Categories": "; ".join(str(item.get("category", "")) for item in categories if isinstance(item, dict)),
                "Category Details": json.dumps(categories, ensure_ascii=False),
                "Most Likely Failure Stage": "; ".join(analysis.get("most_likely_failure_stage", [])),
                "Root Cause": analysis.get("root_cause", ""),
                "Reason": analysis.get("reason", ""),
            }
        )
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(output_rows[0].keys()))
        writer.writeheader()
        writer.writerows(output_rows)


def build_summary(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    category_counter: Counter[str] = Counter()
    stage_counter: Counter[str] = Counter()
    category_stage_counter: Counter[str] = Counter()
    for row in rows:
        analysis = row.get("analysis", {})
        categories = [
            str(item.get("category", "")).strip()
            for item in analysis.get("error_categories", [])
            if isinstance(item, dict) and str(item.get("category", "")).strip()
        ]
        stages = [
            str(stage).strip()
            for stage in analysis.get("most_likely_failure_stage", [])
            if str(stage).strip()
        ]
        for category in categories:
            category_counter[category] += 1
        for stage in stages:
            stage_counter[stage] += 1
        for category in categories:
            for stage in stages:
                category_stage_counter[f"{category}::{stage}"] += 1

    return {
        "sample_count": len(rows),
        "category_counts": dict(category_counter.most_common()),
        "failure_stage_counts": dict(stage_counter.most_common()),
        "category_stage_counts": dict(category_stage_counter.most_common()),
    }


if __name__ == "__main__":
    raise SystemExit(main())
